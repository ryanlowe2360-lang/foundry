"""Plain event records the pipeline consumes. The DXLink feed converts SDK events into these; the
fake feed in tests and the replay tool produce them directly, so nothing downstream depends on the
SDK's pydantic classes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def candle_ticker(event_symbol: str) -> str:
    """'SPY{=1m,tho=true}' → 'SPY'."""
    i = event_symbol.find("{")
    return event_symbol if i < 0 else event_symbol[:i]


@dataclass(slots=True)
class CandleEvt:
    symbol: str          # ticker (already stripped of the {=1m,...} suffix)
    time_ms: int         # bar start, epoch ms
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None
    count: int | None
    kind: str = "Candle"


@dataclass(slots=True)
class QuoteEvt:
    symbol: str
    time_ms: int
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    kind: str = "Quote"


@dataclass(slots=True)
class GreeksEvt:
    symbol: str
    time_ms: int
    price: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    kind: str = "Greeks"


@dataclass(slots=True)
class SummaryEvt:
    symbol: str
    time_ms: int
    open_interest: int | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    prev_close: float | None
    kind: str = "Summary"


@dataclass(slots=True)
class TradeEvt:
    symbol: str
    time_ms: int
    price: float | None
    size: float | None
    day_volume: float | None
    kind: str = "Trade"


@dataclass(slots=True)
class ProfileEvt:
    symbol: str
    time_ms: int
    trading_status: str | None
    halt_start_ms: int | None
    halt_end_ms: int | None
    status_reason: str | None
    kind: str = "Profile"


Evt = CandleEvt | QuoteEvt | GreeksEvt | SummaryEvt | TradeEvt | ProfileEvt

_KINDS: dict[str, type] = {"Candle": CandleEvt, "Quote": QuoteEvt, "Greeks": GreeksEvt, "Summary": SummaryEvt,
                           "Trade": TradeEvt, "Profile": ProfileEvt}


def to_record(e: Evt) -> dict[str, Any]:
    return asdict(e)


def from_record(d: dict[str, Any]) -> Evt:
    cls = _KINDS[d["kind"]]
    return cls(**{k: v for k, v in d.items() if k != "kind"})  # type: ignore[arg-type]


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN → None


def _i(v: Any) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


def from_sdk(ev: Any) -> Evt | None:
    """Convert a tastytrade.dxfeed event to a plain record. Unknown types → None."""
    name = type(ev).__name__
    if name == "Candle":
        return CandleEvt(candle_ticker(ev.event_symbol), int(ev.time), _f(ev.open), _f(ev.high), _f(ev.low), _f(ev.close),
                         _f(ev.volume), _f(ev.vwap), _i(ev.count))
    if name == "Quote":
        t = int(getattr(ev, "bid_time", 0) or getattr(ev, "ask_time", 0) or getattr(ev, "event_time", 0) or 0)
        return QuoteEvt(ev.event_symbol, t, _f(ev.bid_price), _f(ev.ask_price), _f(ev.bid_size), _f(ev.ask_size))
    if name == "Greeks":
        return GreeksEvt(ev.event_symbol, int(ev.time), _f(ev.price), _f(ev.volatility), _f(ev.delta), _f(ev.gamma), _f(ev.theta), _f(ev.vega))
    if name == "Summary":
        return SummaryEvt(ev.event_symbol, int(getattr(ev, "event_time", 0) or 0), _i(ev.open_interest), _f(ev.day_open_price),
                          _f(ev.day_high_price), _f(ev.day_low_price), _f(ev.prev_day_close_price))
    if name == "Trade":
        return TradeEvt(ev.event_symbol, int(ev.time), _f(ev.price), _f(ev.size), _f(ev.day_volume))
    if name == "Profile":
        return ProfileEvt(ev.event_symbol, int(getattr(ev, "event_time", 0) or 0), getattr(ev, "trading_status", None),
                          _i(getattr(ev, "halt_start_time", None)), _i(getattr(ev, "halt_end_time", None)), getattr(ev, "status_reason", None))
    return None
