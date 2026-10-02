"""M3 through the real daemon loop on a virtual clock (the M2 simulation's fakes, extended): the brief's checklists
arrive through `saa_checklists_today`, rules v2 through `saa_rules_latest`, SPY breaks its opening range on volume at
09:36 and runs, option marks refresh every minute, and the engine fires a sized shadow trade plus fast-lane hypotheses.

Asserts the M3 shape end to end: six-gate fire → shadow row marked at bid → trail/time-stop close → rails updated,
decision journal + ledger mirrored through the saa_* RPCs, alerts and engine lines on Telegram, and — the determinism
criterion — the session's own recording replayed through `replay_engine()` reproduces the live decisions and ledger
byte for byte. A second run with a 15-minute-delayed feed proves the gate: observe-only, zero shadow trades.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

import pytest

from saa_daemon import config
from saa_daemon.clock import FakeClock, Schedule, et, et_dt
from saa_daemon.daemon import Daemon
from saa_daemon.engine.replay import replay_engine
from saa_daemon.engine.rules import DEFAULT_PARAMS
from saa_daemon.events import CandleEvt, Evt, GreeksEvt, QuoteEvt, SummaryEvt, TradeEvt
from saa_daemon.feed import FeedPlan
from saa_daemon.mirror import SupabaseMirror
from saa_daemon.store import Store
from saa_daemon.telegram import Notifier
from test_daemon_session_sim import OPT_RE, SPOTS, FakeBrokerage, FakeFeed
from test_daemon_units import FakeHttp


async def _no_broker(_brokerage):
    """M2/M3 simulations: no paper execution (M4 has its own simulation)."""
    return None

D = date(2026, 9, 28)
SCHED = Schedule.for_date(D)


class EngineHttp(FakeHttp):
    def rules_row(self):
        return {"version": 2, "effective_from": "2026-10-01", "params": DEFAULT_PARAMS, "evidence": "M3 build", "created_by": "build-m3"}

    def checklists(self):
        def cl(i, sym, direction, kind, start, end, prob):
            return {"id": i, "trade_date": D.isoformat(), "symbol": sym, "window_kind": kind, "window_start": start.isoformat(), "window_end": end.isoformat(),
                    "catalyst": "test catalyst", "gates": {"catalyst": True, "readable": True, "favorable": True, "wedge": True, "direction": True, "trigger": False},
                    "direction": direction, "probability": prob, "decision": "shadow", "target_r": 5, "structure": "long call 0DTE 0.5σ OTM"}
        open_end = SCHED.open + timedelta(minutes=30)
        return [cl(1, "SPY", "long", "open", SCHED.open, open_end, 0.32),
                cl(2, "NVDA", "long", "open", SCHED.open, open_end, 0.28),
                cl(3, "TSLA", "none", "open", SCHED.open, open_end, 0.20)]      # magnitude only, no two-sided plan → stands down


class EngineFeed(FakeFeed):
    """The M2 fake feed plus: SPY breaks its 5-minute opening range at 09:36 on 2.5× volume and trends up for 20 minutes
    (then the sine path), option quotes/greeks for every planned option refresh every minute, trades carry `lag_ms`."""

    def __init__(self, clock: FakeClock, *, drop_at: datetime | None, lag_ms: int = 0):
        super().__init__(clock, drop_at=drop_at)
        self.lag_ms = lag_ms

    def _price(self, sym: str, minute: int, frac: float) -> float:
        base = SPOTS[sym]
        if sym == "SPY":
            if minute < 6:
                return round(base + (0.08 if (minute + int(frac * 10)) % 2 else -0.08), 2)
            if minute < 26:
                return round(base + 0.25 + 0.22 * (minute - 6) + 0.1 * frac, 2)          # the run: +4.4 points in 20 minutes
            return round(base + 4.65 + 1.4 * math.sin((minute - 26) / 18.0), 2)
        return super()._price(sym, minute, frac)

    def _option_events(self, on_event, ts_ms: int, full: bool, spot_override: dict[str, float] | None = None) -> None:
        for sym in sorted(self.plan.options):
            m_ = OPT_RE.match(sym)
            assert m_, sym
            und, right, strike = m_.group(1), m_.group(3), float(m_.group(4))
            spot = (spot_override or {}).get(und, SPOTS.get(und, 100.0))
            m = abs(strike - spot) / spot
            iv = 0.18 + 0.4 * m + (0.01 if right == "P" else 0.0)
            gamma = max(0.0005, 0.06 * math.exp(-((strike - spot) / (spot * 0.01)) ** 2 / 2))
            intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
            mid = round(intrinsic + 1.6 * math.exp(-m * 60), 2)                  # 0.5σ-OTM ≈ $1.15: inside the one-contract floor
            half = round(max(0.01, mid * 0.02), 2)
            self._emit(on_event, QuoteEvt(sym, ts_ms, max(0.01, mid - half), mid + half, 50, 60))
            self._emit(on_event, GreeksEvt(sym, ts_ms, mid, round(iv, 4), 0.5 if right == "C" else -0.5, round(gamma, 5), -0.1, 0.2))
            if full:
                oi = int(20000 * math.exp(-m * 40)) + (5000 if right == "P" and strike < spot else 0)
                self._emit(on_event, SummaryEvt(sym, ts_ms, oi, None, None, None, None))
                self._emit(on_event, TradeEvt(sym, ts_ms, mid, 1, float(int(oi * 0.3))))

    async def run(self, plan: FeedPlan, on_event: Callable[[Evt], None], stop: asyncio.Event) -> None:
        self.runs += 1
        self.plan = FeedPlan(set(plan.underlyings), set(plan.candles), set(plan.options), plan.candle_start)
        sched = SCHED
        now = self.clock.now()
        ts_ms = int(now.timestamp() * 1000)
        for u in sorted(self.plan.underlyings):
            px = SPOTS[u]
            self._emit(on_event, QuoteEvt(u, ts_ms, px - 0.01, px + 0.01, 100, 100))
            self._emit(on_event, SummaryEvt(u, ts_ms, None, px, None, None, px * 0.998))
            self._emit(on_event, TradeEvt(u, ts_ms - self.lag_ms, px, 100, 1e5))          # dxfeed sends the last trade on subscribe
        self._option_events(on_event, ts_ms, full=True)
        start = self.plan.candle_start or sched.open
        replay_end = min(now, sched.close)
        t = start
        while t < replay_end:
            minute = int((t - sched.open).total_seconds() // 60)
            for s in sorted(self.plan.candles):
                o, c = self._price(s, minute, 0.0), self._price(s, minute, 0.9)
                vol = 2500.0 if (s == "SPY" and minute == 6) else 1000.0
                self._emit(on_event, CandleEvt(s, int(t.timestamp() * 1000), o, max(o, c) + 0.05, min(o, c) - 0.05, c, vol, (o + c) / 2, 40))
            t += timedelta(minutes=1)
        t = max(start, now.replace(second=0, microsecond=0))
        while t < sched.close and not stop.is_set():
            minute = int((t - sched.open).total_seconds() // 60)
            for offs, frac in ((5, 0.1), (30, 0.5), (55, 0.9)):
                wake = t + timedelta(seconds=offs)
                if wake <= self.clock.now():
                    continue
                await self.clock.sleep_until(wake)
                if stop.is_set():
                    return
                if self.drop_at and not self.dropped and wake >= self.drop_at:
                    self.dropped = True
                    raise ConnectionResetError("simulated websocket drop")
                ts_ms = int(wake.timestamp() * 1000)
                spots_now: dict[str, float] = {}
                for s in sorted(self.plan.candles):
                    o, c = self._price(s, minute, 0.0), self._price(s, minute, frac)
                    base_vol = 2500.0 if (s == "SPY" and minute == 6) else 1000.0
                    self._emit(on_event, CandleEvt(s, int(t.timestamp() * 1000), o, max(o, c) + 0.05, min(o, c) - 0.05, c, base_vol * frac, (o + c) / 2, int(40 * frac)))
                    self.candle_symbols_seen.add(s)
                for u in sorted(self.plan.underlyings):
                    px = self._price(u, minute, frac)
                    spots_now[u] = px
                    self._emit(on_event, QuoteEvt(u, ts_ms, px - 0.01, px + 0.01, 100, 100))
                    self._emit(on_event, TradeEvt(u, ts_ms - self.lag_ms, px, 100, 1e6 * (minute + 1)))
                if offs == 55:
                    self._option_events(on_event, ts_ms, full=(minute % 5 == 4), spot_override=spots_now)
            t += timedelta(minutes=1)
        await stop.wait()


async def _run(tmp_path: Path, fixtures: Path, env_file: Path, *, lag_ms: int = 0):
    settings = config.load_settings(env_file, environ={"SAA_MAX_SINGLE_NAMES": "2"}, state_dir=tmp_path / "state")
    clock = FakeClock(et_dt(D, time(9, 15)))
    store = Store(tmp_path / "state" / "saa.sqlite")
    http = EngineHttp(fixtures)
    mirror = SupabaseMirror(settings, store, http)
    notifier = Notifier(settings, mirror, http)
    brokerage = FakeBrokerage()
    feed = EngineFeed(clock, drop_at=et_dt(D, time(11, 0)), lag_ms=lag_ms)
    daemon = Daemon(settings, clock=clock, store=store, http=http, brokerage=brokerage, feed_factory=lambda _b: feed, broker_factory=_no_broker,
                    mirror=mirror, notifier=notifier, mode="session", trade_date=D, host="testmac")
    task = asyncio.create_task(daemon.run_session())
    deadline = SCHED.shutdown + timedelta(minutes=10)
    for _ in range(200):
        await clock.run_until(deadline)
        if task.done():
            break
        await asyncio.sleep(0)
    assert task.done(), "daemon did not finish on the virtual clock"
    return daemon, task.result(), http, store


@pytest.mark.asyncio
async def test_engine_full_session_live_equals_replay(env_file: Path, tmp_path: Path, fixtures: Path):
    daemon, res, http, store = await _run(tmp_path, fixtures, env_file)
    assert res.unhandled == 0 and res.ok and res.errors == {"feed": 1}
    eng = daemon.engine
    assert eng is not None and eng.rules.version == 2 and eng.cfg.account == 1000.0 and eng.cfg.kelly_k == 0.5 and len(eng.checklists) == 3
    st = res.stats["engine"]

    # --- the engine ticked every minute from 09:25, evaluated gates in the entry windows, fired SPY in the open window
    # 09:30:02 is the only tick without a same-session trade sample (the pre-open print is yesterday's close and is ignored)
    assert st["ticks"] >= 410 and st["observe_only_ticks"] <= 1 and st["feed_mode"] == "realtime"
    fired = [p for p in eng.positions.values() if p.source == "gate"]
    assert st["counts"]["fired"] >= 1 and fired and fired[0].symbol == "SPY" and fired[0].window == "open" and fired[0].direction == "long"
    spy = fired[0]
    assert et(spy.opened_at).time() == time(9, 37, 2) and spy.option_type == "call" and spy.option_symbol.startswith(".SPY260928C")
    assert spy.sizing["mode"] == "floor" and spy.contracts == 1 and spy.probability == 0.32 and spy.checklist_id == 1
    assert spy.entry_ask > spy.entry_bid > 0 and spy.gates["all_pass"] and spy.gates["gates"]["trigger"]["note"].startswith("orb")
    assert spy.status == "closed" and spy.exit_reason in ("trail", "time_stop") and spy.marks >= 10 and spy.exit_at <= SCHED.open + timedelta(minutes=30, seconds=2)
    assert spy.hwm_bid > spy.entry_ask and spy.mfe_r > 0                        # the run paid: the mark rose above the entry
    assert st["rails"]["day"]["closed"] == 1 and st["today_gate_r"] == pytest.approx(spy.r_result, abs=1e-4)
    # TSLA: magnitude-only checklist → direction gate fails → stand-down journal row; NVDA: no trigger in the open window
    gate_sd = [d for d in eng.decisions if d["kind"] == "gate" and d["decision"] == "stand_down"]
    assert any(d["symbol"] == "TSLA" and "direction" in d["reason"] for d in gate_sd)
    assert any(d["symbol"] == "NVDA" and "trigger" in d["reason"] for d in gate_sd)
    # fast lanes: the SPY ORB hypothesis plus VWAP flips during the day; every position is closed by the final tick
    assert st["counts"]["fast_lane_opens"] >= 2 and any(p.lane == "orb" for p in eng.positions.values())
    assert st["positions"]["open"] == 0 and all(p.status in ("closed", "void") for p in eng.positions.values())
    assert all(p.exit_at is not None and p.exit_at <= p.time_stop + timedelta(seconds=5) for p in eng.positions.values() if p.status == "closed")
    assert not any(p.opened_at.astimezone(et(SCHED.open).tzinfo).time() >= time(11, 30) and p.opened_at.astimezone(et(SCHED.open).tzinfo).time() < time(13, 30)
                   for p in eng.positions.values())

    # --- persistence + mirror: ledger rows and the journal went out through the RPC surface; SQLite has them too
    calls: dict[str, list[dict]] = {}
    for fn, body in http.calls:
        calls.setdefault(fn, []).append(body)
    pushed = {r["engine_key"]: r for b in calls["saa_engine_shadow_upsert"] for r in b["p_rows"]}
    assert set(pushed) == set(eng.positions) and all(pushed[k]["status"] == eng.positions[k].status for k in pushed)
    assert pushed[spy.key]["r_result"] == pytest.approx(spy.r_result, abs=1e-6) and pushed[spy.key]["model_version"] == "dxlink-marks-v1"
    journal = [r for b in calls["saa_engine_decisions_insert"] for r in b["p_rows"]]
    assert len(journal) == len(eng.decisions) and all(r["trade_date"] == D.isoformat() and r["run_id"] == daemon.run_id for r in journal)
    assert len(store.engine_trades(D.isoformat())) == len(eng.positions) and store.dirty_engine_trades() == [] and store.dirty_engine_decisions() == []
    # alerts on the gate-fired open/close went to Telegram as kind 'alert'; heartbeat + EOD carry the engine lines
    enq = [b for fn, b in http.calls if fn == "saa_enqueue"]
    alerts = [b["p_text"] for b in enq if b["p_kind"] == "alert"]
    assert len(alerts) == 2 and alerts[0].startswith("SHADOW open ▸ SPY long .SPY260928C") and "6/6 gates · p=0.32" in alerts[0] and alerts[1].startswith("SHADOW close ▸ SPY")
    hb = [b["p_text"] for b in enq if b["p_kind"] == "system"][0]
    assert "Engine: feed lag not measured yet (real-time required to evaluate) · rules v2 · acct $1,000 k=0.50 · posterior n=0" in hb and "→ floor" in hb and "4 fast lanes" in hb
    eod = [b["p_text"] for b in enq if b["p_kind"] == "system"][1]
    import re
    assert re.search(r"Engine: rules v2 · (live eval|observe-only 1/\d+ ticks) ·", eod) and "fired" in eod and "Shadow R: gate" in eod and "Stand-downs:" in eod
    session_log = calls["saa_log_run"][-1]
    assert session_log["p_detail"]["engine"]["rules_version"] == 2 and session_log["p_detail"]["engine"]["counts"]["fired"] == st["counts"]["fired"]
    for s in config.load_settings(env_file, environ={}, state_dir=tmp_path).secret_values():
        assert s not in str(http.calls)

    # --- determinism: replaying the session's own recording reproduces the live engine byte for byte
    rec = Path(res.stats["recording"])
    assert rec.is_file()
    kinds = {}
    for ln in rec.read_text(encoding="utf-8").splitlines():
        k = json.loads(ln)["kind"]
        kinds[k] = kinds.get(k, 0) + 1
    assert kinds["Meta"] == 1 and kinds["Plan"] == 5 and kinds["Tick"] == st["ticks"] and kinds["OptMarks"] >= 5 * 400 and kinds["Candle"] > 5000
    rep1 = replay_engine(rec)
    rep2 = replay_engine(rec)
    assert rep1.canonical == rep2.canonical and rep1.ticks == st["ticks"] and not rep1.synthesized_ticks
    assert rep1.feed_modes.get("realtime", 0) >= 400 and rep1.feed_modes.get("unknown", 0) <= 11      # pre-open ticks: no same-session trade yet
    live = json.loads(eng.canonical())
    replayed = json.loads(rep1.canonical)
    assert replayed["decisions"] == live["decisions"]
    assert replayed["ledger"] == live["ledger"]
    assert replayed["state"] == live["state"]
    assert rep1.summary()["fired"] == st["counts"]["fired"] and rep1.summary()["sha256"] == rep2.summary()["sha256"]


@pytest.mark.asyncio
async def test_engine_delayed_feed_is_observe_only(env_file: Path, tmp_path: Path, fixtures: Path):
    daemon, res, http, store = await _run(tmp_path, fixtures, env_file, lag_ms=900_000)
    assert res.unhandled == 0 and res.ok
    st = res.stats["engine"]
    assert res.stats["feed"]["mode"] == "DELAYED" and res.stats["feed"]["lag_s"] >= 899
    assert st["feed_mode"] == "DELAYED" and st["observe_only_ticks"] >= 380 and st["counts"]["fired"] == 0 and st["counts"]["fast_lane_opens"] == 0
    assert daemon.engine is not None and not daemon.engine.positions
    assert all(d["decision"] == "stand_down" and d["reason"].startswith("feed_not_realtime") for d in daemon.engine.decisions)
    assert {d["symbol"] for d in daemon.engine.decisions} == {"SPY", "QQQ", "IWM", "NVDA", "TSLA"}
    enq = [b for fn, b in http.calls if fn == "saa_enqueue"]
    assert not [b for b in enq if b["p_kind"] == "alert"]
    hb, eod = [b["p_text"] for b in enq if b["p_kind"] == "system"]
    assert "Engine: feed lag not measured yet" in hb                      # 09:25: no same-session trade yet, by design
    assert "OBSERVE-ONLY all day (feed DELAYED)" in eod and "0 fired" in eod
    assert not [b for fn, b in http.calls if fn == "saa_engine_shadow_upsert"]
    # the recording still replays deterministically and reports the delayed feed
    rep = replay_engine(Path(res.stats["recording"]))
    assert rep.feed_modes.get("DELAYED", 0) >= 380 and rep.summary()["fired"] == 0 and rep.canonical == replay_engine(Path(res.stats["recording"])).canonical
