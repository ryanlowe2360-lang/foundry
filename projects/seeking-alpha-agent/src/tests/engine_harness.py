"""Shared builders for the M3 engine tests: a MarketState with chain plans, two-sided option marks and 1-minute bars
shaped on demand (opening-range breakout, flat tape, VWAP loss), plus a one-call `tick()`.

Everything is deterministic — no randomness here; Hypothesis supplies the randomness in the property tests."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from saa_daemon.chains import ChainPlan, ExpPlan, OptState, StrikePlan
from saa_daemon.clock import Schedule, et_dt
from saa_daemon.engine import Engine, EngineConfig, EngineInputs, Rules
from saa_daemon.events import CandleEvt
from saa_daemon.market import MarketState

D = date(2026, 9, 28)   # Monday
SCHED = Schedule.for_date(D)


def at(hh: int, mm: int, ss: int = 0, d: date = D) -> datetime:
    return et_dt(d, time(hh, mm, ss))


def make_plan(symbol: str, spot: float, *, step: float = 1.0, n_side: int = 12, exps: Iterable[date] = (D, D + timedelta(days=1))) -> ChainPlan:
    ks = [round(spot // step * step + i * step, 2) for i in range(-n_side, n_side + 1)]
    out = []
    for e in exps:
        code = f"{e:%y%m%d}"
        out.append(ExpPlan(e, (e - D).days, tuple(StrikePlan(k, f".{symbol}{code}C{k:g}", f".{symbol}{code}P{k:g}") for k in ks)))
    return ChainPlan(symbol, tuple(out))


def set_marks(market: MarketState, plan: ChainPlan, spot: float, now: datetime, *, spread_frac: float = 0.04, iv: float = 0.20,
              premium_atm: float = 1.00, gamma: float = 0.05, oi: float = 1000.0) -> None:
    """Two-sided marks for every planned option: premium decays with moneyness, spread = spread_frac × ask."""
    recv = int(now.timestamp() * 1000)
    for e in plan.expirations:
        for s in e.strikes:
            for osym, right in ((s.call, "C"), (s.put, "P")):
                m = abs(s.strike - spot) / spot
                intrinsic = max(0.0, spot - s.strike) if right == "C" else max(0.0, s.strike - spot)
                mid = round(intrinsic + premium_atm * (2.718 ** (-m * 60)), 2)
                mid = max(mid, 0.05)
                ask = round(mid / (1 - spread_frac / 2), 2)
                bid = round(max(0.01, ask * (1 - spread_frac)), 2)
                st = market.options.state.setdefault(osym, OptState())
                st.bid, st.ask, st.iv, st.delta, st.gamma, st.oi, st.volume = bid, ask, iv, (0.5 if right == "C" else -0.5), gamma, oi, 100.0
                st.quote_ms, st.recv_ms = recv, recv


def make_market(symbols: dict[str, float], now: datetime, **mark_kw: Any) -> MarketState:
    m = MarketState.new()
    for sym, spot in symbols.items():
        plan = make_plan(sym, spot)
        m.plans[sym] = plan
        m.spots[sym] = spot
        m.underlyings.add(sym)
        set_marks(m, plan, spot, now, **mark_kw)
    return m


def candle(sym: str, start: datetime, o: float, h: float, l: float, c: float, vol: float) -> CandleEvt:
    return CandleEvt(sym, int(start.timestamp() * 1000), o, h, l, c, vol, (o + c) / 2, int(vol // 25) or 1)


def flat_bars(market: MarketState, sym: str, spot: float, start: datetime, minutes: int, *, vol: float = 1000.0) -> None:
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        market.bars.on_candle(candle(sym, t, spot, spot + 0.05, spot - 0.05, spot, vol))


def orb_breakout_bars(market: MarketState, sym: str, spot: float, start: datetime, minutes: int, *, break_at: int = 6, direction: str = "long",
                      vol: float = 1000.0) -> None:
    """5 range bars at `spot` ± 0.1, then at bar `break_at` a close outside the range on 2× volume, then drift."""
    sign = 1 if direction == "long" else -1
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        if i < break_at:
            market.bars.on_candle(candle(sym, t, spot, spot + 0.1, spot - 0.1, spot + (0.05 if i % 2 else -0.05), vol))
        elif i == break_at:
            c = spot + sign * 0.6
            market.bars.on_candle(candle(sym, t, spot, max(spot, c) + 0.05, min(spot, c) - 0.05, c, vol * 2.2))
        else:
            c = spot + sign * (0.6 + 0.05 * (i - break_at))
            market.bars.on_candle(candle(sym, t, c - sign * 0.02, max(c, c - sign * 0.02) + 0.03, min(c, c - sign * 0.02) - 0.03, c, vol * 1.1))


def vwap_reclaim_bars(market: MarketState, sym: str, spot: float, start: datetime, minutes: int, *, vol: float = 1000.0) -> None:
    """A steady drift down (every close below the running VWAP), then the last bar closes sharply back above VWAP → `vwap long`."""
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        if i < minutes - 1:
            c = spot - 1.0 * (i + 1) / max(1, minutes - 1)
            market.bars.on_candle(candle(sym, t, c + 0.03, c + 0.06, c - 0.03, c, vol))
        else:
            market.bars.on_candle(candle(sym, t, spot - 0.9, spot + 0.55, spot - 0.95, spot + 0.5, vol * 2.0))


def checklist(sym: str, direction: str = "long", *, window_kind: str = "open", probability: float = 0.30, gates: dict[str, bool] | None = None,
              cid: int = 1, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    g = {"catalyst": True, "readable": True, "favorable": True, "wedge": True, "direction": True, "trigger": False}
    if gates:
        g.update(gates)
    row = {"id": cid, "symbol": sym, "window_kind": window_kind, "direction": direction, "probability": probability, "decision": "shadow",
           "gates": g, "target_r": 5, "structure": "long call, nearest expiry, 0.5σ OTM", "catalyst": "earnings BMO (test)"}
    if start and end:
        row["window_start"], row["window_end"] = start.isoformat(), end.isoformat()
    return row


def make_engine(*, checklists: Iterable[dict[str, Any]] = (), econ: Iterable[dict[str, Any]] = (), history: Iterable[float] = (), cooling_off: bool = False,
                account: float = 1000.0, k: float = 0.5, index_symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM"), require_realtime: bool = True,
                rules: Rules | None = None, trade_date: date = D) -> Engine:
    cfg = EngineConfig(trade_date=trade_date, account=account, kelly_k=k, index_symbols=index_symbols, require_realtime=require_realtime)
    return Engine(cfg, rules or Rules.default(), checklists=list(checklists), econ_events=list(econ), history_r=list(history), cooling_off=cooling_off)


NEG_GAMMA = {"regime": "negative", "coverage": 1.0, "flip": 651.0, "call_wall": 655.0, "put_wall": 645.0}
VIX = {"vix": 14.9, "vix1d": 12.5, "vix9d": 12.8, "vix3m": 17.9, "shape": "contango"}


def tick(engine: Engine, market: MarketState, now: datetime, *, feed_mode: str = "realtime", lag_s: float | None = 1.0,
         gamma: dict[str, dict[str, Any]] | None = None, vix: dict[str, Any] | None = VIX, universe: list[str] | None = None) -> list[dict[str, Any]]:
    uni = universe if universe is not None else sorted(market.plans)
    g = gamma if gamma is not None else {s: NEG_GAMMA for s in uni}
    market.bars.complete_before(now)
    return engine.on_minute(EngineInputs(now=now, feed_mode=feed_mode, lag_s=lag_s, vix=vix, gamma=g, universe=uni), market)
