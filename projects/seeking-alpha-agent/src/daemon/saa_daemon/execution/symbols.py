"""Option symbol conversion and price ticks.

The daemon streams and decides on dxfeed *streamer* symbols (`.SPY260928C650`, `.NVDA261016P177.5`); the tastytrade
order API wants OCC 2010 symbols (`SPY   260928C00650000` — root padded to 6, yymmdd, C/P, strike × 1000 in 8 digits).
Pure functions, no SDK import, so the whole order path is testable without tastytrade installed.
"""
from __future__ import annotations

import math
import re
from datetime import date

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


def tick_size(price: float) -> float:
    """Penny-pilot option classes (SPY/QQQ/IWM and the liquid single names): $0.01 under $3.00, $0.05 at/above."""
    return 0.01 if price < 3.0 else 0.05


def round_to_tick(price: float, side: str) -> float:
    """Round a price onto the tick grid *toward the far side*: a buyer rounds up, a seller rounds down. Never below one tick."""
    if price <= 0:
        return 0.01
    t = tick_size(price)
    n = price / t
    k = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    out = round(k * t, 2)
    return max(out, 0.01)


def nearest_tick(price: float) -> float:
    """Round a price to the nearest tick (intermediate ladder rungs)."""
    if price <= 0:
        return 0.01
    t = tick_size(price)
    return max(round(round(price / t) * t, 2), 0.01)


__all__ = ["parse_streamer", "streamer_to_occ", "occ_to_streamer", "tick_size", "round_to_tick", "nearest_tick"]
