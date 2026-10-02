"""The engine tick: once a minute, manage open shadow positions, then consider entries — the six-gate path (sized by the
Kelly module, counted by the rails) and the fast-lane hypotheses (one contract, zero capital, journal only).

Order inside a tick is fixed (positions first, then entries, symbols sorted) so a replay reproduces the live run exactly.
The engine never reads a clock, never fetches, never randomizes. Everything it decides is appended to `decisions` (the
journal — stand-downs included, they *are* the record) and `positions` (the shadow ledger).
"""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from ..chains import OptState
from ..clock import et
from ..market import INDEX_SIGMA_MULT, MarketState
from .gates import ChainRead, GateResult, evaluate_gates
from .kelly import Posterior, Sizing, posterior, size_position
from .positions import Exit, Position
from .rails import Rails
from .rules import Rules, parse_hhmm
from .tier1 import TIER1, Tier1
from .triggers import Signal, continuation_signal, day_bars, orb_signal, rvol_signal, vwap_signal
from .windows import Window, WindowSchedule, theta_clock

FAST_LANES = ("orb", "vwap", "continuation", "rvol")


@dataclass(frozen=True)
class EngineConfig:
    trade_date: date
    account: float
    kelly_k: float
    index_symbols: tuple[str, ...]
    require_realtime: bool = True      # live: no entries unless feed_lag.mode == realtime
    mark_max_age_s: int = 180          # an option mark received more than 3 minutes ago is stale → no entry on it
    tier1: Tier1 = TIER1


@dataclass
class EngineInputs:
    now: datetime
    feed_mode: str                     # realtime | DELAYED | unknown | replay
    lag_s: float | None
    vix: dict[str, Any] | None
    gamma: dict[str, dict[str, Any]]   # latest dealer-gamma proxy per underlying
    universe: list[str]


@dataclass(frozen=True)
class Selected:
    option_symbol: str
    option_type: str
    strike: float
    expiration: date
    dte: int
    mark: OptState
    sigma_d: float
    atm_iv: float | None
    spread_flag: bool


class Engine:
    def __init__(self, cfg: EngineConfig, rules: Rules, *, checklists: Iterable[dict[str, Any]] = (), econ_events: Iterable[dict[str, Any]] = (),
                 history_r: Iterable[float] = (), cooling_off: bool = False, baseline_volume: dict[str, dict[int, float]] | None = None):
        self.cfg = cfg
        self.rules = rules
        self.tier1 = cfg.tier1
        self.sched = WindowSchedule.build(cfg.trade_date, rules, list(econ_events), tier1=cfg.tier1)
        self.rails = Rails(cfg.tier1, [float(r) for r in history_r], cooling_off)
        self.checklists: list[dict[str, Any]] = [c for c in checklists if isinstance(c, dict) and c.get("symbol")]
        self.baseline_volume = baseline_volume or {}
        self.positions: dict[str, Position] = {}
        self.decisions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []        # opens / closes / alerts for the daemon to deliver
        self.ticks = 0
        self.entry_ticks = 0           # ticks at/after the open (where entries are evaluated)
        self.observe_only_ticks = 0
        self.last_feed_mode: str | None = None
        self.counts: dict[str, int] = {"evaluations": 0, "fired": 0, "fast_lane_opens": 0, "closes": 0, "alerts": 0}
        self.stand_down_reasons: dict[str, int] = {}
        self._recorded: set[tuple[str, str, str]] = set()
        self._fired: set[tuple[str, str]] = set()        # (symbol, window.name) — one gate position per window per symbol per day
        self._lane_done: set[tuple[str, str, str]] = set()  # (symbol, lane, direction) per day
        self._alerts_today = 0

    # ---------------------------------------------------------------------------------------------------- helpers
    def is_index(self, symbol: str) -> bool:
        return symbol in self.cfg.index_symbols

    def posterior(self) -> Posterior:
        return posterior(self.rails.history, self.tier1)

    def open_positions(self, *, source: str | None = None) -> list[Position]:
        return [p for k, p in sorted(self.positions.items()) if p.status == "open" and (source is None or p.source == source)]

    def _record(self, now: datetime, symbol: str, window: str, kind: str, decision: str, reason: str, *, once: bool = False, **extra: Any) -> None:
        if once:
            key = (symbol, window, reason.split(":")[0])
            if key in self._recorded:
                return
            self._recorded.add(key)
        row = {"ts": now.isoformat(), "et": et(now).strftime("%H:%M"), "symbol": symbol, "window": window, "kind": kind, "decision": decision,
               "reason": reason, "theta_clock": theta_clock(now, self.sched.open, self.sched.close)}
        row.update(extra)
        self.decisions.append(row)
        if decision == "stand_down":
            k = reason.split(":")[0]
            self.stand_down_reasons[k] = self.stand_down_reasons.get(k, 0) + 1

    def _checklist_for(self, symbol: str, window: Window, now: datetime) -> dict[str, Any] | None:
        rows = [c for c in self.checklists if str(c.get("symbol", "")).upper() == symbol]
        if not rows:
            return None

        def ts(v: Any) -> datetime | None:
            if not v:
                return None
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                return None

        timed = [c for c in rows if ts(c.get("window_start")) and ts(c.get("window_end")) and ts(c["window_start"]) <= now < ts(c["window_end"])]
        if timed:
            return timed[0]
        kind = "event" if window.kind in ("event", "presser") else window.kind
        by_kind = [c for c in rows if c.get("window_kind") == kind]
        if by_kind:
            return by_kind[0]
        return None

    def _chain_read(self, symbol: str, market: MarketState) -> ChainRead | None:
        plan = market.plans.get(symbol)
        spot = market.spots.get(symbol)
        if plan is None or not spot or not plan.expirations:
            return None
        pct = float(self.rules.shadow.get("mark_window_pct", 1.5))
        lo, hi = spot * (1 - pct / 100.0), spot * (1 + pct / 100.0)
        n = quoted = greeked = 0
        atm_ivs: list[float] = []
        e0 = plan.expirations[0]
        atm = min(e0.strikes, key=lambda s: abs(s.strike - spot)) if e0.strikes else None
        for e in plan.expirations:
            for s in e.strikes:
                if not (lo <= s.strike <= hi):
                    continue
                for osym in (s.call, s.put):
                    st = market.options.get(osym)
                    n += 1
                    if st is not None and st.two_sided:
                        quoted += 1
                    if st is not None and st.gamma is not None:
                        greeked += 1
                    if atm is not None and e is e0 and s is atm and st is not None and st.iv:
                        atm_ivs.append(st.iv)
        if n == 0:
            return None
        return ChainRead(quoted / n, greeked / n, (sum(atm_ivs) / len(atm_ivs)) if atm_ivs else None, n)

    def _sigma_d(self, symbol: str, atm_iv: float | None, vix: dict[str, Any] | None) -> float:
        if atm_iv and atm_iv > 0:
            return atm_iv / math.sqrt(252.0)
        if self.is_index(symbol) and vix:
            base = vix.get("vix1d") or vix.get("vix")
            if base:
                return float(base) / 100.0 / math.sqrt(252.0) * INDEX_SIGMA_MULT.get(symbol, 1.0)
        return float(self.rules.shadow["single_name_default_sigma_d"])

    def _select_option(self, symbol: str, direction: str, market: MarketState, vix: dict[str, Any] | None, now: datetime) -> tuple[Selected | None, str]:
        plan = market.plans.get(symbol)
        spot = market.spots.get(symbol)
        if plan is None or not spot:
            return None, "no chain plan or spot"
        live = [e for e in plan.expirations if e.expiration >= self.cfg.trade_date and e.strikes]
        if not live:
            return None, "no live expiration"
        e = live[0]
        chain = self._chain_read(symbol, market)
        sigma_d = self._sigma_d(symbol, chain.atm_iv if chain else None, vix)
        sign = 1.0 if direction == "long" else -1.0
        target = spot * (1 + sign * float(self.rules.shadow["strike_sigma_otm"]) * sigma_d)
        sp = min(e.strikes, key=lambda s: (abs(s.strike - target), s.strike))
        osym = sp.call if direction == "long" else sp.put
        st = market.options.get(osym)
        if st is None or not st.two_sided:
            return None, f"no two-sided mark for {osym}"
        if st.recv_ms and (now.timestamp() * 1000 - st.recv_ms) > self.cfg.mark_max_age_s * 1000:
            return None, f"stale mark for {osym} ({(now.timestamp() * 1000 - st.recv_ms) / 1000:.0f}s old)"
        sf = st.spread_frac or 0.0
        if sf > self.tier1.spread_skip_frac:
            return None, f"spread {sf:.0%} of premium > {self.tier1.spread_skip_frac:.0%} ({osym} {st.bid:.2f}/{st.ask:.2f})"
        return Selected(osym, "call" if direction == "long" else "put", sp.strike, e.expiration, e.dte, st, sigma_d, chain.atm_iv if chain else None,
                        sf > self.tier1.spread_flag_frac), ""

    # ------------------------------------------------------------------------------------------------------- tick
    def on_minute(self, inp: EngineInputs, market: MarketState) -> list[dict[str, Any]]:
        self.ticks += 1
        self.last_feed_mode = inp.feed_mode
        before = len(self.events)
        now = inp.now
        self._manage(now, market)
        if now >= self.sched.open:
            self._entries(inp, market)
        return self.events[before:]

    # --------------------------------------------------------------------------------------------- manage exits
    def _manage(self, now: datetime, market: MarketState) -> None:
        for pos in self.open_positions():
            st = market.options.get(pos.option_symbol)
            exit_: Exit | None = None
            if st is not None and st.two_sided:
                banked_before = pos.banked_contracts
                exit_ = pos.on_mark(now, st.bid, st.ask, market.spots.get(pos.symbol), self.rules)
                if pos.banked_contracts > banked_before:
                    # partial bank: the daemon's paper executor (M4) sells these contracts; the journal row is in pos.events
                    self.events.append({"type": "bank", "at": now.isoformat(), "position": pos.key, "symbol": pos.symbol, "source": pos.source,
                                        "contracts": pos.banked_contracts - banked_before, "bid": pos.last_bid})
            if exit_ is None:
                bars = day_bars(market.bars, pos.symbol, self.sched.open, now)
                exit_ = pos.on_bar_close(bars, self.rules)
            if exit_ is None:
                exit_ = pos.time_exit(now)
            if exit_ is None:
                continue
            if pos.marks == 0 and pos.last_bid is None:
                pos.void(now, "no mark ever received")
                self._record(now, pos.symbol, pos.window, "manage", "void", "void_no_marks", position=pos.key)
                continue
            pos.close(now, exit_)
            notes = self.rails.on_close(pos.r_result or 0.0, now, counts=pos.counts_for_rails)
            self.counts["closes"] += 1
            self._record(now, pos.symbol, pos.window, "manage", "close", f"{exit_.reason}: {exit_.note}", position=pos.key,
                         r_result=round(pos.r_result or 0.0, 4), rails_notes=notes)
            ev = {"type": "close", "at": now.isoformat(), "position": pos.key, "symbol": pos.symbol, "source": pos.source, "lane": pos.lane,
                  "r_result": round(pos.r_result or 0.0, 4), "reason": exit_.reason, "row": pos.row()}
            self.events.append(ev)
            if pos.source == "gate":
                self._alert(now, f"SHADOW close ▸ {pos.symbol} {pos.option_symbol} {pos.r_result:+.2f}R ({exit_.reason})"
                            + (f" · {'; '.join(notes)}" if notes else ""))

    def _alert(self, now: datetime, text: str) -> None:
        if self._alerts_today >= self.rules.max_alerts_per_day:
            return
        self._alerts_today += 1
        self.counts["alerts"] += 1
        self.events.append({"type": "alert", "at": now.isoformat(), "text": text})

    # ---------------------------------------------------------------------------------------------------- entries
    def _entries(self, inp: EngineInputs, market: MarketState) -> None:
        now = inp.now
        universe = sorted(set(inp.universe))
        if not universe:
            return
        self.entry_ticks += 1
        if self.cfg.require_realtime and inp.feed_mode != "realtime":
            self.observe_only_ticks += 1
            w = self.sched.regular_window(now)
            wname = w.name if w else "—"
            for sym in universe:
                self._record(now, sym, wname, "gate", "stand_down", f"feed_not_realtime: mode {inp.feed_mode}, lag {inp.lag_s}s — observe only", once=True,
                             feed_mode=inp.feed_mode, lag_s=inp.lag_s)
            return
        window, why = self.sched.entry_window(now, tier1=self.tier1)
        halted = market.halted()
        for sym in universe:
            if sym in halted:
                self._record(now, sym, window.name if window else "—", "gate", "stand_down", "halted: trading status HALTED", once=True)
                continue
            bars = day_bars(market.bars, sym, self.sched.open, now)
            signals = self._signals(sym, bars, now)
            self._fast_lanes(sym, signals, now, market, inp)
            if window is None:
                if signals:
                    self._record(now, sym, "—", "gate", "stand_down", f"clock: {why}", once=True, signals=[s.as_dict() for s in signals])
                continue
            self._gate_path(sym, window, bars, signals, now, market, inp)

    def _signals(self, sym: str, bars: list, now: datetime) -> list[Signal]:
        out: list[Signal] = []
        lanes = self.rules.fast_lanes
        t = et(now).time()
        cfg = lanes.get("orb") or {}
        if cfg.get("enabled") and t <= parse_hhmm(cfg["entry_until_et"]):
            s = orb_signal(bars, range_minutes=int(cfg["range_minutes"]), volume_mult=float(cfg["volume_mult"]))
            if s:
                out.append(s)
        cfg = lanes.get("vwap") or {}
        if cfg.get("enabled") and t <= parse_hhmm(cfg["entry_until_et"]):
            s = vwap_signal(bars, min_bars=int(cfg["min_bars"]))
            if s:
                out.append(s)
        cfg = lanes.get("continuation") or {}
        if cfg.get("enabled") and t <= parse_hhmm(cfg["entry_until_et"]):
            s = continuation_signal(bars, start_et=parse_hhmm(cfg["start_et"]), lookback=int(cfg["lookback"]), volume_mult=float(cfg["volume_mult"]))
            if s:
                out.append(s)
        cfg = lanes.get("rvol") or {}
        if cfg.get("enabled") and t <= parse_hhmm(cfg["entry_until_et"]):
            s = rvol_signal(bars, self.baseline_volume.get(sym), mult=float(cfg["mult"]))
            if s:
                out.append(s)
        return out

    def _clock_ok_for_lane(self, now: datetime) -> tuple[Window | None, str]:
        """Fast lanes obey the Tier 1 clock (dead zone, blackouts, last 5 minutes) but not the regular windows' entry cut-offs."""
        if now < self.sched.open:
            return None, "pre-open"
        if now >= self.sched.close - timedelta(minutes=self.tier1.last_entry_before_close_minutes):
            return None, "last 5 minutes"
        b = self.sched.blackout_at(now)
        if b is not None:
            return None, f"release blackout ({b.event})"
        if self.sched.in_dead_zone(now, self.tier1):
            return None, "Tier 1 dead zone 11:30–13:30"
        w = self.sched.event_window(now) or self.sched.regular_window(now)
        if w is None:
            return None, "no window"
        return w, ""

    def _fast_lanes(self, sym: str, signals: list[Signal], now: datetime, market: MarketState, inp: EngineInputs) -> None:
        for s in signals:
            lane_cfg = self.rules.lane(s.lane) or {}
            if not lane_cfg.get("opens_shadow"):
                self._record(now, sym, "—", "fast_lane", "flag_only", f"{s.lane}: lane does not open shadows", signal=s.as_dict())
                continue
            key = (sym, s.lane, s.direction)
            if key in self._lane_done:
                continue
            w, why = self._clock_ok_for_lane(now)
            if w is None:
                self._lane_done.add(key)
                self._record(now, sym, "—", "fast_lane", "skip", f"clock: {why}", signal=s.as_dict())
                continue
            sel, why = self._select_option(sym, s.direction, market, inp.vix, now)
            if sel is None:
                self._lane_done.add(key)
                self._record(now, sym, w.name, "fast_lane", "skip", f"marks: {why}", signal=s.as_dict())
                continue
            self._lane_done.add(key)
            contracts = int(self.rules.shadow["contracts"])
            sizing = {"contracts": contracts, "mode": "hypothesis", "reasons": ["fast-lane hypothesis: zero capital, journal only"]}
            pos = self._open(sym, "fast_lane", s.lane, w, s.direction, sel, contracts, sizing, now, s, None, counts=False, spot=market.spots.get(sym) or 0.0)
            self.counts["fast_lane_opens"] += 1
            self._record(now, sym, w.name, "fast_lane", "open", f"{s.lane}: {s.note}", position=pos.key, signal=s.as_dict())

    def _gate_path(self, sym: str, window: Window, bars: list, signals: list[Signal], now: datetime, market: MarketState, inp: EngineInputs) -> None:
        if (sym, window.name) in self._fired:
            return
        ok, why = self.rails.can_enter()
        if not ok:
            self._record(now, sym, window.name, "gate", "stand_down", f"rails: {why}", once=True)
            return
        if len(self.open_positions(source="gate")) >= self.rules.max_concurrent:
            self._record(now, sym, window.name, "gate", "stand_down", f"capacity: {self.rules.max_concurrent} concurrent positions", once=True)
            return
        if window.requires_negative_gamma:
            g = inp.gamma.get(sym if not self.is_index(sym) else (self.cfg.index_symbols[0] if self.cfg.index_symbols else sym)) or {}
            if g.get("regime") != "negative":
                self._record(now, sym, window.name, "gate", "stand_down", f"gamma: last hour needs a negative-gamma read (proxy {g.get('regime') or 'unknown'})", once=True)
                return
        is_idx = self.is_index(sym)
        gamma = inp.gamma.get(sym) if not is_idx else inp.gamma.get(self.cfg.index_symbols[0] if self.cfg.index_symbols else sym)
        if gamma is None:
            gamma = inp.gamma.get(sym)
        checklist = self._checklist_for(sym, window, now)
        chain = self._chain_read(sym, market)
        release_passed = window.kind in ("event", "presser") and now >= window.start
        self.counts["evaluations"] += 1
        res: GateResult = evaluate_gates(symbol=sym, is_index=is_idx, now=now, window=window, checklist=checklist, gamma=gamma, vix=inp.vix,
                                         chain=chain, bars=bars, signals=signals, rules=self.rules, release_passed=release_passed)
        if not res.all_pass:
            failed = "+".join(res.failed())
            self._record(now, sym, window.name, "gate", "stand_down", f"gates: {res.passed}/6 — failed {failed}", once=True, gates=res.as_dict())
            return
        legs = ["long", "short"] if res.direction == "two_sided" else [res.direction]
        opened = 0
        for direction in legs:
            sel, why = self._select_option(sym, direction, market, inp.vix, now)
            if sel is None:
                self._record(now, sym, window.name, "gate", "stand_down", f"marks: {why}", once=True, gates=res.as_dict())
                continue
            post = self.posterior()
            if res.direction == "two_sided":
                sizing = Sizing(1, sel.mark.ask * 100.0, sel.mark.ask * 100.0, sel.mark.ask * 100.0 / self.cfg.account, post.f_full, 0.0, self.cfg.kelly_k,
                                "floor", ("two-sided structure: one contract per leg",))
            else:
                sizing = size_position(self.cfg.account, sel.mark.ask, post, k=self.cfg.kelly_k, halt_mode=self.rails.halt_mode(),
                                       cooling_off=self.rails.cooling_off_today, tier1=self.tier1)
            if sizing.contracts <= 0:
                self._record(now, sym, window.name, "gate", "stand_down", f"size: {'; '.join(sizing.reasons)} ({sel.option_symbol} @ {sel.mark.ask:.2f})", once=True,
                             gates=res.as_dict(), sizing=sizing.as_dict(), posterior=post.as_dict())
                continue
            pos = self._open(sym, "gate", window.kind, window, direction, sel, sizing.contracts, sizing.as_dict(), now, res.trigger, res, counts=True,
                             spot=market.spots.get(sym) or 0.0)
            opened += 1
            self.counts["fired"] += 1
            self._record(now, sym, window.name, "gate", "fire", f"6/6 gates · {sizing.mode} · {sizing.contracts}×{sel.option_symbol} @ {sel.mark.ask:.2f}",
                         position=pos.key, gates=res.as_dict(), sizing=sizing.as_dict(), posterior=post.as_dict())
            self._alert(now, f"SHADOW open ▸ {sym} {direction} {sel.option_symbol} ×{sizing.contracts} @ {sel.mark.ask:.2f} (R ${sizing.risk_dollars:.0f}, {sizing.mode})"
                        f" · {window.name} · 6/6 gates · p={res.probability if res.probability is not None else '?'} · stop {et(pos.time_stop):%H:%M}"
                        + (" · spread flagged" if sel.spread_flag else ""))
        if opened:
            self._fired.add((sym, window.name))

    def _open(self, sym: str, source: str, lane: str, window: Window, direction: str, sel: Selected, contracts: int, sizing: dict[str, Any],
              now: datetime, trigger: Signal | None, res: GateResult | None, *, counts: bool, spot: float) -> Position:
        stop = self.sched.time_stop_for(window, now, tier1=self.tier1)
        nb = self.sched.next_blackout_after(now)
        close_cut = self.sched.close - timedelta(minutes=self.tier1.last_entry_before_close_minutes)
        if nb is not None and stop == nb.start and stop < window.stop:
            stop_reason = "release"
        elif stop == close_cut and window.stop > close_cut:
            stop_reason = "close"
        else:
            stop_reason = "window"
        key = f"{self.cfg.trade_date.isoformat()}|{sym}|{source}|{lane}|{direction}|{et(now):%H%M}"
        pos = Position(key=key, trade_date=self.cfg.trade_date, symbol=sym, is_index=self.is_index(sym), source=source, lane=lane, window=window.name,
                       direction=direction, option_symbol=sel.option_symbol, option_type=sel.option_type, strike=sel.strike, expiration=sel.expiration,
                       dte=sel.dte, opened_at=now, entry_bar_time=trigger.bar_time if trigger else now, entry_bid=float(sel.mark.bid), entry_ask=float(sel.mark.ask),
                       entry_iv=sel.mark.iv, entry_spot=float(spot), sigma_d=sel.sigma_d, contracts=contracts,
                       time_stop=stop, time_stop_reason=stop_reason, account=self.cfg.account, sizing=sizing,
                       probability=res.probability if res else None, checklist_id=res.checklist_id if res else None, gates=res.as_dict() if res else None,
                       counts_for_rails=counts)
        pos.diffs = deque(maxlen=int(self.rules.trail["atr_ticks"]))
        self.positions[key] = pos
        self.events.append({"type": "open", "at": now.isoformat(), "position": key, "symbol": sym, "source": source, "lane": lane, "row": pos.row()})
        return pos

    # ----------------------------------------------------------------------------------------------- end of day
    def close_all(self, now: datetime, reason: str = "session_end") -> None:
        for pos in self.open_positions():
            if pos.marks == 0:
                pos.void(now, "no mark ever received")
            else:
                pos.close(now, Exit(reason, "forced at session end"))
                self.rails.on_close(pos.r_result or 0.0, now, counts=pos.counts_for_rails)
                self.counts["closes"] += 1
            self._record(now, pos.symbol, pos.window, "manage", "close" if pos.status == "closed" else "void", reason, position=pos.key,
                         r_result=round(pos.r_result or 0.0, 4) if pos.r_result is not None else None)
            self.events.append({"type": "close", "at": now.isoformat(), "position": pos.key, "symbol": pos.symbol, "source": pos.source, "lane": pos.lane,
                                "r_result": round(pos.r_result or 0.0, 4), "reason": reason, "row": pos.row()})

    # ----------------------------------------------------------------------------------------------------- output
    def ledger_rows(self) -> list[dict[str, Any]]:
        return [self.positions[k].row() for k in sorted(self.positions)]

    def state(self) -> dict[str, Any]:
        post = self.posterior()
        today_gate = [p for p in self.positions.values() if p.source == "gate" and p.status == "closed"]
        return {
            "version": 1, "rules": self.rules.summary(), "account": self.cfg.account, "kelly_k": self.cfg.kelly_k, "require_realtime": self.cfg.require_realtime,
            "feed_mode": self.last_feed_mode, "ticks": self.ticks, "entry_ticks": self.entry_ticks, "observe_only_ticks": self.observe_only_ticks,
            "posterior": post.as_dict(), "rails": self.rails.state(), "counts": dict(self.counts),
            "stand_down_reasons": dict(sorted(self.stand_down_reasons.items())),
            "positions": {"open": len(self.open_positions()), "closed": sum(1 for p in self.positions.values() if p.status == "closed"),
                          "void": sum(1 for p in self.positions.values() if p.status == "void"),
                          "gate": sum(1 for p in self.positions.values() if p.source == "gate"),
                          "fast_lane": sum(1 for p in self.positions.values() if p.source == "fast_lane")},
            "today_gate_r": round(sum(p.r_result or 0.0 for p in today_gate), 4),
            "today_fast_lane_r": round(sum(p.r_result or 0.0 for p in self.positions.values() if p.source == "fast_lane" and p.status == "closed"), 4),
            "windows": self.sched.as_dict(),
        }

    def canonical(self, meta: dict[str, Any] | None = None) -> bytes:
        """Byte-stable JSON of everything the engine produced (sorted keys, rounded floats). Same inputs → same bytes."""
        doc = {"meta": meta or {}, "decisions": self.decisions, "ledger": self.ledger_rows(), "state": self.state()}
        return (json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str) + "\n").encode("utf-8")
