"""In-memory market state shared by the live daemon and the replay tool: the bar book, the option book, spots and
underlying quotes, the feed-lag samples — plus the per-minute **mark digest** the engine decides on.

The digest is the contract between live and replay: at each engine tick the live daemon snapshots the planned
near-ATM option marks per underlying into a compact record, writes it to the recording, and hands the same record to
the engine. Replay reads the record back into the option book and hands it to the engine. Same bytes in → same
decisions out.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .bars import BarBook
from .chains import ChainPlan, OptState, OptionBook, STRIKE_COLUMNS
from .events import CandleEvt, Evt, GreeksEvt, ProfileEvt, QuoteEvt, SummaryEvt, TradeEvt

MARK_COLUMNS = ("bid", "ask", "iv", "delta", "gamma", "theta", "oi", "volume", "quote_ms", "recv_ms")
INDEX_SIGMA_MULT = {"SPY": 1.0, "QQQ": 1.2, "IWM": 1.3}   # same proxies as saa.sigma_d_for (M1)


def mark_to_list(s: OptState) -> list[Any]:
    return [s.bid, s.ask, s.iv, s.delta, s.gamma, s.theta, s.oi, s.volume, s.quote_ms, s.recv_ms]


def list_to_mark(v: list[Any]) -> OptState:
    s = OptState()
    s.bid, s.ask, s.iv, s.delta, s.gamma, s.theta, s.oi, s.volume = v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]
    s.quote_ms = int(v[8] or 0)
    s.recv_ms = int(v[9] or 0) if len(v) > 9 else 0
    return s


@dataclass
class MarketState:
    bars: BarBook
    options: OptionBook
    plans: dict[str, ChainPlan]
    spots: dict[str, float]
    quotes: dict[str, dict[str, Any]]
    lag_samples: deque
    underlyings: set[str]
    feed_events: int = 0
    on_profile: Callable[[ProfileEvt], None] | None = None

    @classmethod
    def new(cls, *, on_profile: Callable[[ProfileEvt], None] | None = None) -> "MarketState":
        return cls(BarBook(), OptionBook(), {}, {}, {}, deque(maxlen=400), set(), 0, on_profile)

    # ------------------------------------------------------------------------------------------------- event path
    def on_event(self, e: Evt, now: datetime) -> None:
        self.feed_events += 1
        recv_ms = int(now.timestamp() * 1000)
        if isinstance(e, CandleEvt):
            self.bars.on_candle(e)
        elif isinstance(e, QuoteEvt):
            if e.symbol in self.underlyings:
                q = self.quotes.setdefault(e.symbol, {})
                q.update(bid=e.bid, ask=e.ask, bid_size=e.bid_size, ask_size=e.ask_size, ts=e.time_ms)
                if e.bid and e.ask:
                    self.spots[e.symbol] = (e.bid + e.ask) / 2.0
            else:
                self.options.on_quote(e, recv_ms)
        elif isinstance(e, GreeksEvt):
            self.options.on_greeks(e)
        elif isinstance(e, SummaryEvt):
            if e.symbol in self.underlyings:
                q = self.quotes.setdefault(e.symbol, {})
                q.update(day_open=e.day_open, prev_close=e.prev_close)
            else:
                self.options.on_summary(e)
        elif isinstance(e, TradeEvt):
            if e.symbol in self.underlyings:
                if e.time_ms > 0:
                    self.lag_samples.append(now.timestamp() - e.time_ms / 1000.0)
                q = self.quotes.setdefault(e.symbol, {})
                q.update(last=e.price, day_volume=e.day_volume)
                if e.price and e.symbol not in self.spots:
                    self.spots[e.symbol] = e.price
            else:
                self.options.on_trade(e)
        elif isinstance(e, ProfileEvt):
            q = self.quotes.setdefault(e.symbol, {})
            q.update(trading_status=e.trading_status)
            if self.on_profile is not None:
                self.on_profile(e)

    def feed_lag(self) -> dict[str, Any]:
        """Median exchange→receipt delay of underlying trades. ~1 s = real-time; ~900 s = the 15-minute delayed feed."""
        if not self.lag_samples:
            return {"lag_s": None, "mode": "unknown", "n": 0}
        med = statistics.median(self.lag_samples)
        return {"lag_s": round(med, 1), "mode": "realtime" if med < 30 else "DELAYED", "n": len(self.lag_samples)}

    def halted(self) -> set[str]:
        return {s for s, q in self.quotes.items() if (q.get("trading_status") or "").upper() == "HALTED"}

    # ----------------------------------------------------------------------------------------------------- digest
    def digest(self, symbol: str, pct: float) -> dict[str, Any] | None:
        """Marks of the planned options within ±pct% of spot, every planned expiration. None without a plan or spot."""
        plan = self.plans.get(symbol)
        spot = self.spots.get(symbol)
        if plan is None or not spot:
            return None
        lo, hi = spot * (1 - pct / 100.0), spot * (1 + pct / 100.0)
        marks: dict[str, list[Any]] = {}
        for e in plan.expirations:
            for s in e.strikes:
                if lo <= s.strike <= hi:
                    for sym in (s.call, s.put):
                        st = self.options.get(sym)
                        if st is not None:
                            marks[sym] = mark_to_list(st)
        return {"symbol": symbol, "spot": round(spot, 4), "marks": {k: marks[k] for k in sorted(marks)}}

    def apply_digest(self, rec: dict[str, Any]) -> None:
        """Replay: load a recorded digest into the option book (and the spot)."""
        sym = rec["symbol"]
        if rec.get("spot"):
            self.spots[sym] = float(rec["spot"])
        for osym, v in (rec.get("marks") or {}).items():
            self.options.state[osym] = list_to_mark(v)


def plan_to_record(plan: ChainPlan) -> dict[str, Any]:
    return {"symbol": plan.underlying, "expirations": [{"exp": e.expiration.isoformat(), "dte": e.dte,
                                                        "strikes": [[s.strike, s.call, s.put] for s in e.strikes]} for e in plan.expirations]}


def plan_from_record(rec: dict[str, Any]) -> ChainPlan:
    from datetime import date

    from .chains import ExpPlan, StrikePlan
    exps = []
    for e in rec["expirations"]:
        exps.append(ExpPlan(date.fromisoformat(e["exp"]), int(e["dte"]), tuple(StrikePlan(float(k), c, p) for k, c, p in e["strikes"])))
    return ChainPlan(rec["symbol"], tuple(exps))


__all__ = ["MarketState", "MARK_COLUMNS", "STRIKE_COLUMNS", "INDEX_SIGMA_MULT", "mark_to_list", "list_to_mark", "plan_to_record", "plan_from_record"]
