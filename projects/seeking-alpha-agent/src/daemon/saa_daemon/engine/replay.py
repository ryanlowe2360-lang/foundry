"""Deterministic replay of a recorded session through the engine.

A recording (state/recordings/<run>.jsonl) holds, in receipt order: the underlying-level market events (M2), and — since
M3 — `Meta` (the engine's session inputs), `Plan` (chain plans), `OptMarks` (the per-minute mark digest) and `Tick`
(the engine tick: feed mode, VIX, gamma proxies, universe). Replaying feeds every record into a fresh MarketState and
runs `Engine.on_minute()` at every Tick exactly as the live daemon did, so the output bytes equal the live run's.

A pre-M3 recording (no Meta/Tick, e.g. the real 2026-09-28 session) still replays: ticks are synthesized at each new
receipt minute, the feed mode comes from the recording's own trade timestamps, and the engine runs with default inputs
(no checklists → every gate path stands down; the bar triggers and the journal are still produced deterministically).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..clock import et, floor_minute
from ..events import from_record, ms_to_dt
from ..market import MarketState, plan_from_record
from .engine import Engine, EngineConfig, EngineInputs
from .rules import Rules

ENGINE_RECORD_KINDS = ("Meta", "Plan", "OptMarks", "Tick")
TICK_OFFSET_S = 2          # the live engine ticks 2 s into each minute, after the previous bar has completed


@dataclass
class ReplayResult:
    engine: Engine
    meta: dict[str, Any]
    records: int
    ticks: int
    synthesized_ticks: bool
    feed_modes: dict[str, int]
    canonical: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()

    def summary(self) -> dict[str, Any]:
        st = self.engine.state()
        return {"records": self.records, "ticks": self.ticks, "synthesized_ticks": self.synthesized_ticks, "feed_modes": self.feed_modes,
                "decisions": len(self.engine.decisions), "ledger_rows": len(self.engine.positions), "fired": st["counts"]["fired"],
                "fast_lane_opens": st["counts"]["fast_lane_opens"], "closes": st["counts"]["closes"], "stand_down_reasons": st["stand_down_reasons"],
                "today_gate_r": st["today_gate_r"], "today_fast_lane_r": st["today_fast_lane_r"], "sha256": self.sha256,
                "trade_date": self.meta.get("trade_date"), "rules_version": self.meta.get("rules", {}).get("version")}


def _default_meta(path: Path, first_recv_ms: int, symbols: list[str], *, trade_date: date | None, require_realtime: bool) -> dict[str, Any]:
    d = trade_date or et(ms_to_dt(first_recv_ms)).date()
    idx = [s for s in ("SPY", "QQQ", "IWM") if s in symbols] or symbols[:3]
    return {"kind": "Meta", "synthetic": True, "recording": path.name, "trade_date": d.isoformat(), "account": 1000.0, "kelly_k": 0.5,
            "index_symbols": idx, "universe": symbols, "rules": {"version": 0, "params": None}, "checklists": [], "econ": [], "history_r": [],
            "cooling_off": False, "require_realtime": require_realtime}


def build_engine(meta: dict[str, Any], *, require_realtime: bool | None = None) -> Engine:
    rules = Rules.from_row(meta.get("rules")) if meta.get("rules", {}).get("params") else Rules.default()
    cfg = EngineConfig(trade_date=date.fromisoformat(meta["trade_date"]), account=float(meta.get("account") or 1000.0), kelly_k=float(meta.get("kelly_k") or 0.5),
                       index_symbols=tuple(meta.get("index_symbols") or ()), require_realtime=bool(meta.get("require_realtime", True)) if require_realtime is None else require_realtime)
    return Engine(cfg, rules, checklists=meta.get("checklists") or [], econ_events=meta.get("econ") or [], history_r=meta.get("history_r") or [],
                  cooling_off=bool(meta.get("cooling_off")))


def replay_engine(path: Path, *, require_realtime: bool | None = None, trade_date: date | None = None) -> ReplayResult:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    has_ticks = any('"kind":"Tick"' in ln for ln in lines)
    market = MarketState.new()
    engine: Engine | None = None
    meta: dict[str, Any] | None = None
    ticks = 0
    feed_modes: dict[str, int] = {}
    last_minute: datetime | None = None
    last_now: datetime | None = None
    symbols_seen: list[str] = []

    def ensure_engine(now_ms: int) -> Engine:
        nonlocal engine, meta
        if engine is None:
            # a pre-M3 file has no Meta: the live default (gate on) applies unless the caller lifts it (--eval)
            meta = _default_meta(path, now_ms, sorted(set(symbols_seen)), trade_date=trade_date, require_realtime=(True if require_realtime is None else require_realtime))
            engine = build_engine(meta, require_realtime=require_realtime)
            market.underlyings |= set(meta["universe"])
        return engine

    def tick(now: datetime, rec: dict[str, Any] | None) -> None:
        nonlocal ticks
        eng = ensure_engine(int(now.timestamp() * 1000))
        market.bars.complete_before(now)
        if rec is not None:
            mode, lag = rec.get("feed_mode") or "unknown", rec.get("lag_s")
            vix, gamma, universe = rec.get("vix"), rec.get("gamma") or {}, rec.get("universe") or sorted(market.underlyings)
        else:
            fl = market.feed_lag()
            mode, lag, vix, gamma, universe = fl["mode"], fl["lag_s"], None, {}, sorted(market.underlyings)
        feed_modes[mode] = feed_modes.get(mode, 0) + 1
        eng.on_minute(EngineInputs(now=now, feed_mode=mode, lag_s=lag, vix=vix, gamma=gamma, universe=list(universe)), market)
        ticks += 1
        if rec is not None and rec.get("final"):
            eng.close_all(now)

    for ln in lines:
        rec = json.loads(ln)
        kind = rec.get("kind")
        recv = int(rec.get("recv_ms") or rec.get("time_ms") or 0)
        now = ms_to_dt(recv) if recv else (last_now or ms_to_dt(0))
        if kind == "Meta":
            meta = rec
            engine = build_engine(meta, require_realtime=require_realtime)
            market.underlyings |= set(meta.get("universe") or [])
        elif kind == "Plan":
            market.plans[rec["symbol"]] = plan_from_record(rec)
            market.underlyings.add(rec["symbol"])
        elif kind == "OptMarks":
            market.apply_digest(rec)
        elif kind == "Tick":
            tick(now, rec)
        else:
            rec.pop("recv_ms", None)
            e = from_record(rec)
            if e.symbol not in symbols_seen and e.kind in ("Candle", "Quote", "Trade", "Summary", "Profile"):
                symbols_seen.append(e.symbol)
                market.underlyings.add(e.symbol)
            if not has_ticks and recv:
                minute = floor_minute(now)
                if last_minute is not None and minute > last_minute:
                    # synthesize the live cadence: one tick 2 s into every new receipt minute
                    tick(last_minute + timedelta(minutes=1, seconds=TICK_OFFSET_S), None)
                last_minute = minute
            market.on_event(e, now)
        last_now = now
    if engine is None:
        ensure_engine(int((last_now or ms_to_dt(0)).timestamp() * 1000))
    assert engine is not None and meta is not None
    if not has_ticks and last_now is not None:
        tick(floor_minute(last_now) + timedelta(minutes=1, seconds=TICK_OFFSET_S), None)
        engine.close_all(floor_minute(last_now) + timedelta(minutes=1, seconds=TICK_OFFSET_S))
    canon = engine.canonical({k: v for k, v in meta.items() if k not in ("checklists",)} | {"records": len(lines), "ticks": ticks, "synthesized_ticks": not has_ticks})
    return ReplayResult(engine, meta, len(lines), ticks, not has_ticks, feed_modes, canon)
