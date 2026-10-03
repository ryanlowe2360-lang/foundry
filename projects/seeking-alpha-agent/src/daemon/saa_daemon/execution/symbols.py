"""Option symbol conversion and price ticks (a per-class tick table fed from the broker's chain).

The daemon streams and decides on dxfeed *streamer* symbols (`.SPY260928C650`, `.NVDA261016P177.5`); the tastytrade
order API wants OCC 2010 symbols (`SPY   260928C00650000` — root padded to 6, yymmdd, C/P, strike × 1000 in 8 digits).
Pure functions, no SDK import, so the whole order path is testable without tastytrade installed.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

STREAMER_RE = re.compile(r"^\.([A-Z]{1,6})(\d{6})([CP])(\d+)(?:\.(\d+))?$")
OCC_RE = re.compile(r"^([A-Z]{1,6}) *(\d{6})([CP])(\d{8})$")


def parse_streamer(sym: str) -> tuple[str, date, str, float]:
    """`.SPY260928C650.5` → ("SPY", 2026-09-28, "C", 650.5)."""
    m = STREAMER_RE.match(sym or "")
    if m is None:
        raise ValueError(f"not a streamer option symbol: {sym!r}")
    und, ymd, right, whole, frac = m.groups()
    strike = float(whole) + (float(f"0.{frac}") if frac else 0.0)
    return und, date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])), right, strike


def streamer_to_occ(sym: str) -> str:
    und, exp, right, strike = parse_streamer(sym)
    return f"{und.ljust(6)}{exp:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def occ_to_streamer(occ: str) -> str:
    m = OCC_RE.match(occ or "")
    if m is None:
        raise ValueError(f"not an OCC option symbol: {occ!r}")
    und, ymd, right, strike_k = m.groups()
    strike = int(strike_k) / 1000.0
    s = f"{strike:.3f}".rstrip("0").rstrip(".")
    return f".{und}{ymd}{right}{s}"


# ------------------------------------------------------------------------------------------------- tick table
# Exchange price increments by option class. The Penny Interval Program classes (the liquid names) trade in $0.01 under
# $3.00 and $0.05 at/above — except SPY, QQQ and IWM (and Cboe's XSP), which trade in $0.01 at every price; classes
# outside the program trade in $0.05 / $0.10. The nested option chain the broker serves carries the authoritative rules
# (`tick_sizes: [{value, threshold?}]`) and `set_ticks_from_chain` registers them whenever a chain is fetched, so the
# order path rounds to the grid the exchange will accept. Rules are (tick, threshold): the tick applies while
# price < threshold; `None` = the last tick.
TickRules = tuple[tuple[float, float | None], ...]
PENNY_PROGRAM: TickRules = ((0.01, 3.0), (0.05, None))
PENNY_ALL: TickRules = ((0.01, None),)
_TICKS: dict[str, TickRules] = {"SPY": PENNY_ALL, "QQQ": PENNY_ALL, "IWM": PENNY_ALL, "XSP": PENNY_ALL}


def root_of(symbol: str | None) -> str | None:
    """The option root of a streamer symbol (`.SPY260928C654`), an OCC symbol (`SPY   260928C00654000`) or a bare root."""
    if not symbol:
        return None
    m = STREAMER_RE.match(symbol)
    if m is not None:
        return m.group(1)
    m = OCC_RE.match(symbol)
    if m is not None:
        return m.group(1)
    return symbol.strip().upper() or None


def set_ticks(root: str, rules: list[tuple[float, float | None]] | TickRules) -> TickRules:
    """Register the tick rules for an option root; a rule set without a final unbounded tick keeps the default tail."""
    cleaned = sorted(((float(t), (float(th) if th is not None else None)) for t, th in rules if t and float(t) > 0),
                     key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0))
    if not cleaned:
        return ticks_for(root)
    if cleaned[-1][1] is not None:
        cleaned.append((cleaned[-1][0], None))
    _TICKS[root.upper()] = tuple(cleaned)
    return _TICKS[root.upper()]


def set_ticks_from_chain(nested: Any) -> TickRules | None:
    """Register the rules a tastytrade `NestedOptionChain` carries (`root_symbol`, `tick_sizes`). Returns them, or None."""
    sizes = list(getattr(nested, "tick_sizes", None) or [])
    root = getattr(nested, "root_symbol", None) or getattr(nested, "underlying_symbol", None)
    if not sizes or not root:
        return None
    rules = []
    for ts in sizes:
        v, th = getattr(ts, "value", None), getattr(ts, "threshold", None)
        if v is None:
            continue
        rules.append((float(v), float(th) if th is not None else None))
    return set_ticks(str(root), rules) if rules else None


def ticks_for(symbol: str | None) -> TickRules:
    root = root_of(symbol)
    return _TICKS.get(root, PENNY_PROGRAM) if root else PENNY_PROGRAM


def tick_size(price: float, symbol: str | None = None) -> float:
    """The price increment for `symbol` (root, OCC or streamer symbol; the penny-program rule when unknown) at `price`."""
    for tick, threshold in ticks_for(symbol):
        if threshold is None or price < threshold - 1e-9:
            return tick
    return ticks_for(symbol)[-1][0]


def round_to_tick(price: float, side: str, symbol: str | None = None) -> float:
    """Round a price onto the tick grid *toward the far side*: a buyer rounds up, a seller rounds down. Never below one tick."""
    if price <= 0:
        return 0.01
    t = tick_size(price, symbol)
    n = price / t
    k = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    out = round(k * t, 2)
    return max(out, 0.01)


def nearest_tick(price: float, symbol: str | None = None) -> float:
    """Round a price to the nearest tick (intermediate ladder rungs)."""
    if price <= 0:
        return 0.01
    t = tick_size(price, symbol)
    return max(round(round(price / t) * t, 2), 0.01)


__all__ = ["parse_streamer", "streamer_to_occ", "occ_to_streamer", "root_of", "set_ticks", "set_ticks_from_chain", "ticks_for",
           "tick_size", "round_to_tick", "nearest_tick", "PENNY_PROGRAM", "PENNY_ALL"]
