"""Option chains: which expirations/strikes to stream, the in-memory option book, and the 5-minute
snapshot (compact arrays + summary + dealer-gamma proxy). Pure Python apart from `from_sdk_nested`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Iterable

from .events import GreeksEvt, QuoteEvt, SummaryEvt, TradeEvt
from .gamma import GexInput, dealer_gamma_proxy

# Compact strike row layout stored in saa.chain_snapshots.expirations[*].strikes[*]
STRIKE_COLUMNS = ("strike", "call_bid", "call_ask", "call_iv", "call_delta", "call_gamma", "call_oi", "call_volume",
                  "put_bid", "put_ask", "put_iv", "put_delta", "put_gamma", "put_oi", "put_volume")


@dataclass(frozen=True, slots=True)
class StrikePlan:
    strike: float
    call: str        # streamer symbol, e.g. .SPY260928C650
    put: str


@dataclass(frozen=True, slots=True)
class ExpPlan:
    expiration: date
    dte: int
    strikes: tuple[StrikePlan, ...]

    def symbols(self) -> list[str]:
        out: list[str] = []
        for s in self.strikes:
            out.append(s.call)
            out.append(s.put)
        return out


@dataclass(frozen=True, slots=True)
class ChainPlan:
    underlying: str
    expirations: tuple[ExpPlan, ...]

    def symbols(self) -> list[str]:
        return [s for e in self.expirations for s in e.symbols()]

    @property
    def n_strikes(self) -> int:
        return sum(len(e.strikes) for e in self.expirations)


@dataclass
class RawExpiration:
    """Adapter shape for one expiration of a nested chain (SDK-independent)."""
    expiration_date: date
    days_to_expiration: int
    strikes: list[tuple[float, str, str]]   # (strike, call_streamer_symbol, put_streamer_symbol)


def from_sdk_nested(nested: Any) -> list[RawExpiration]:
    """tastytrade.instruments.NestedOptionChain (one root) → RawExpiration list."""
    out: list[RawExpiration] = []
    for e in nested.expirations:
        strikes = [(float(s.strike_price), s.call_streamer_symbol, s.put_streamer_symbol) for s in e.strikes
                   if s.call_streamer_symbol and s.put_streamer_symbol]
        out.append(RawExpiration(e.expiration_date, int(e.days_to_expiration), strikes))
    return out


def plan_chain(underlying: str, expirations: Iterable[RawExpiration], spot: float, *, today: date, now_et: time,
               n_exp: int, window_pct: float, max_per_side: int, min_strikes: int = 3) -> ChainPlan:
    """Pick the nearest `n_exp` live expirations and the strikes around spot.

    A same-day expiration is skipped once the ET clock is at/after 16:00 (it has expired). Strikes inside
    spot·(1 ± window%) are taken, nearest first, capped at `max_per_side` per side; if the window holds fewer than
    `min_strikes` (coarse strike steps), the nearest `max_per_side` per side are taken regardless of the window.
    """
    live = [e for e in expirations if e.expiration_date > today or (e.expiration_date == today and now_et < time(16, 0))]
    live.sort(key=lambda e: e.expiration_date)
    picked: list[ExpPlan] = []
    lo, hi = spot * (1 - window_pct / 100.0), spot * (1 + window_pct / 100.0)
    for e in live[:max(0, n_exp)]:
        strikes = sorted(e.strikes, key=lambda s: s[0])
        below = [s for s in strikes if s[0] <= spot]
        above = [s for s in strikes if s[0] > spot]
        in_below = [s for s in below if s[0] >= lo][-max_per_side:]
        in_above = [s for s in above if s[0] <= hi][:max_per_side]
        if len(in_below) + len(in_above) < min_strikes:
            in_below, in_above = below[-max_per_side:], above[:max_per_side]
        chosen = in_below + in_above
        if not chosen:
            continue
        dte = max(0, (e.expiration_date - today).days)
        picked.append(ExpPlan(e.expiration_date, dte, tuple(StrikePlan(k, c, p) for k, c, p in chosen)))
    return ChainPlan(underlying.upper(), tuple(picked))


@dataclass(slots=True)
class OptState:
    bid: float | None = None
    ask: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    oi: float | None = None
    volume: float | None = None
    quote_ms: int = 0
    greeks_ms: int = 0

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


class OptionBook:
    """Latest known state per option streamer symbol."""

    def __init__(self) -> None:
        self.state: dict[str, OptState] = {}
        self.events = 0

    def _get(self, sym: str) -> OptState:
        s = self.state.get(sym)
        if s is None:
            s = OptState()
            self.state[sym] = s
        return s

    def on_quote(self, e: QuoteEvt) -> None:
        s = self._get(e.symbol)
        s.bid, s.ask, s.quote_ms = e.bid, e.ask, e.time_ms
        self.events += 1

    def on_greeks(self, e: GreeksEvt) -> None:
        s = self._get(e.symbol)
        s.iv, s.delta, s.gamma, s.theta, s.vega, s.greeks_ms = e.iv, e.delta, e.gamma, e.theta, e.vega, e.time_ms
        self.events += 1

    def on_summary(self, e: SummaryEvt) -> None:
        s = self._get(e.symbol)
        if e.open_interest is not None:
            s.oi = float(e.open_interest)
        self.events += 1

    def on_trade(self, e: TradeEvt) -> None:
        s = self._get(e.symbol)
        if e.day_volume is not None:
            s.volume = e.day_volume
        self.events += 1

    def get(self, sym: str) -> OptState | None:
        return self.state.get(sym)


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None else round(v, nd)


def build_snapshot(plan: ChainPlan, book: OptionBook, spot: float | None, ts: datetime) -> dict[str, Any]:
    """One underlying's snapshot: {ts, underlying, spot, expirations:[{exp, dte, strikes:[[...15 cols]]}], summary, gamma}."""
    exps: list[dict[str, Any]] = []
    gex_rows: list[GexInput] = []
    call_oi = put_oi = call_vol = put_vol = 0.0
    quoted = greeked = oi_known = total = 0
    atm_ivs: list[float] = []
    for e in plan.expirations:
        rows: list[list[Any]] = []
        atm = min(e.strikes, key=lambda s: abs(s.strike - spot)) if spot and e.strikes else None
        for s in e.strikes:
            c = book.get(s.call) or OptState()
            p = book.get(s.put) or OptState()
            rows.append([s.strike, _r(c.bid, 2), _r(c.ask, 2), _r(c.iv, 4), _r(c.delta, 4), _r(c.gamma, 5), _r(c.oi, 0), _r(c.volume, 0),
                         _r(p.bid, 2), _r(p.ask, 2), _r(p.iv, 4), _r(p.delta, 4), _r(p.gamma, 5), _r(p.oi, 0), _r(p.volume, 0)])
            for st, right in ((c, "C"), (p, "P")):
                total += 1
                quoted += st.bid is not None and st.ask is not None
                greeked += st.gamma is not None
                oi_known += st.oi is not None
                gex_rows.append(GexInput(s.strike, right, st.gamma, st.oi))
            call_oi += c.oi or 0.0
            put_oi += p.oi or 0.0
            call_vol += c.volume or 0.0
            put_vol += p.volume or 0.0
            if atm is not None and s is atm:
                atm_ivs.extend(v for v in (c.iv, p.iv) if v is not None)
        exps.append({"exp": e.expiration.isoformat(), "dte": e.dte, "strikes": rows})
    summary = {
        "atm_iv": round(sum(atm_ivs) / len(atm_ivs), 4) if atm_ivs else None,
        "call_oi": round(call_oi), "put_oi": round(put_oi), "pc_oi": round(put_oi / call_oi, 3) if call_oi else None,
        "call_vol": round(call_vol), "put_vol": round(put_vol), "pc_vol": round(put_vol / call_vol, 3) if call_vol else None,
        "n_strikes": plan.n_strikes, "n_exp": len(plan.expirations), "n_options": total,
        "coverage": {"quotes": round(quoted / total, 3) if total else 0.0, "greeks": round(greeked / total, 3) if total else 0.0,
                     "oi": round(oi_known / total, 3) if total else 0.0},
    }
    gamma = dealer_gamma_proxy(gex_rows, spot)
    return {"ts": ts, "underlying": plan.underlying, "spot": spot, "expirations": exps, "summary": summary, "gamma": gamma}
