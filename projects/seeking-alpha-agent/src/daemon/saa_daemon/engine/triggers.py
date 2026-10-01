"""Native triggers from the daemon's own 1-minute bars (plan §7: "the engine computes the same triggers natively from
1-min broker bars in M3+ so nothing external sits in the live path"). Pure functions over completed bars.

Lanes (Pine v1 equivalents, same names as `saa.rules.fast_lanes` and `saa.triggers.lane`):
* orb — the last completed bar closes outside the opening range (first N minutes) with volume ≥ mult × the OR bars' average.
* vwap — reclaim (prior close below session VWAP, this close above → long) / loss (→ short), after min_bars bars.
* continuation — after 10:00: a new session high (long) / low (short) on volume ≥ mult × the prior `lookback` bars' average.
* rvol — time-of-day-normalized relative volume ≥ mult; needs a per-minute baseline from `saa.bars_1m` history (20 days);
  without a baseline the lane is inert and says so.
Plus the measurements the gates and exits use: session VWAP, realized vol (annualized) over the last N minutes, failed new
extreme ("pushes sold into"), volume taper.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Sequence

from ..bars import Bar, BarBook
from ..clock import et

MINUTES_PER_YEAR = 252 * 390


@dataclass(frozen=True)
class Signal:
    lane: str
    direction: str        # long | short
    price: float
    bar_time: datetime    # start of the bar that fired
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {"lane": self.lane, "direction": self.direction, "price": round(self.price, 4), "bar_time": et(self.bar_time).strftime("%H:%M"), "note": self.note}


def day_bars(book: BarBook, symbol: str, open_: datetime, now: datetime) -> list[Bar]:
    """Completed regular-session bars for `symbol` whose minute has ended by `now`, oldest first."""
    cutoff = now - timedelta(minutes=1)
    out = [b for (s, t), b in book.bars.items() if s == symbol and open_ <= t <= cutoff and b.close is not None]
    out.sort(key=lambda b: b.start)
    return out


def typical(b: Bar) -> float:
    if b.vwap is not None:
        return b.vwap
    hs = [x for x in (b.high, b.low, b.close) if x is not None]
    return sum(hs) / len(hs) if hs else float(b.close or 0.0)


def session_vwap(bars: Sequence[Bar]) -> float | None:
    num = den = 0.0
    for b in bars:
        v = b.volume or 0.0
        if v <= 0:
            continue
        num += typical(b) * v
        den += v
    if den <= 0:
        return (sum(float(b.close) for b in bars) / len(bars)) if bars else None
    return num / den


def _avg_volume(bars: Sequence[Bar]) -> float:
    vs = [b.volume or 0.0 for b in bars]
    return sum(vs) / len(vs) if vs else 0.0


def opening_range(bars: Sequence[Bar], minutes: int) -> tuple[float, float, float] | None:
    orb = bars[:minutes]
    if len(orb) < minutes:
        return None
    hi = max(b.high if b.high is not None else b.close for b in orb)
    lo = min(b.low if b.low is not None else b.close for b in orb)
    return float(hi), float(lo), _avg_volume(orb)


def orb_signal(bars: Sequence[Bar], *, range_minutes: int, volume_mult: float) -> Signal | None:
    orr = opening_range(bars, range_minutes)
    if orr is None or len(bars) <= range_minutes:
        return None
    hi, lo, avg_vol = orr
    last = bars[-1]
    vol_ok = (last.volume or 0.0) >= volume_mult * avg_vol and avg_vol > 0
    if not vol_ok:
        return None
    if last.close > hi:
        return Signal("orb", "long", float(last.close), last.start, f"close {last.close:.2f} > OR high {hi:.2f}, vol {(last.volume or 0) / avg_vol:.1f}× OR avg")
    if last.close < lo:
        return Signal("orb", "short", float(last.close), last.start, f"close {last.close:.2f} < OR low {lo:.2f}, vol {(last.volume or 0) / avg_vol:.1f}× OR avg")
    return None


def vwap_signal(bars: Sequence[Bar], *, min_bars: int) -> Signal | None:
    if len(bars) < max(min_bars, 2):
        return None
    prev_vwap = session_vwap(bars[:-1])
    cur_vwap = session_vwap(bars)
    if prev_vwap is None or cur_vwap is None:
        return None
    prev, last = bars[-2], bars[-1]
    if prev.close < prev_vwap and last.close > cur_vwap:
        return Signal("vwap", "long", float(last.close), last.start, f"reclaim: {prev.close:.2f} < {prev_vwap:.2f} → {last.close:.2f} > {cur_vwap:.2f}")
    if prev.close > prev_vwap and last.close < cur_vwap:
        return Signal("vwap", "short", float(last.close), last.start, f"loss: {prev.close:.2f} > {prev_vwap:.2f} → {last.close:.2f} < {cur_vwap:.2f}")
    return None


def continuation_signal(bars: Sequence[Bar], *, start_et: time, lookback: int, volume_mult: float) -> Signal | None:
    if len(bars) <= lookback:
        return None
    last = bars[-1]
    if et(last.start).time() < start_et:
        return None
    prior = bars[:-1]
    hi = max((b.high if b.high is not None else b.close) for b in prior)
    lo = min((b.low if b.low is not None else b.close) for b in prior)
    avg_vol = _avg_volume(prior[-lookback:])
    if avg_vol <= 0 or (last.volume or 0.0) < volume_mult * avg_vol:
        return None
    if last.close > hi:
        return Signal("continuation", "long", float(last.close), last.start, f"new session high {last.close:.2f} > {hi:.2f} on {(last.volume or 0) / avg_vol:.1f}× vol")
    if last.close < lo:
        return Signal("continuation", "short", float(last.close), last.start, f"new session low {last.close:.2f} < {lo:.2f} on {(last.volume or 0) / avg_vol:.1f}× vol")
    return None


def relative_volume(bars: Sequence[Bar], baseline_by_minute: dict[int, float] | None) -> float | None:
    """Σ volume so far ÷ the baseline's Σ volume over the same minutes of the day (time-of-day normalized). None without a baseline."""
    if not baseline_by_minute or not bars:
        return None
    have = 0.0
    base = 0.0
    for i, b in enumerate(bars):
        have += b.volume or 0.0
        base += baseline_by_minute.get(i, 0.0)
    return (have / base) if base > 0 else None


def rvol_signal(bars: Sequence[Bar], baseline_by_minute: dict[int, float] | None, *, mult: float) -> Signal | None:
    rv = relative_volume(bars, baseline_by_minute)
    if rv is None or rv < mult or len(bars) < 2:
        return None
    last, prev = bars[-1], bars[-2]
    direction = "long" if last.close >= prev.close else "short"
    return Signal("rvol", direction, float(last.close), last.start, f"RVOL {rv:.1f}× (time-of-day normalized)")


def realized_vol_annualized(bars: Sequence[Bar], minutes: int) -> float | None:
    """Close-to-close realized vol over the last `minutes` bars, annualized (√(252·390) per minute)."""
    xs = [float(b.close) for b in bars[-(minutes + 1):] if b.close]
    if len(xs) < max(10, minutes // 3):
        return None
    rets = [math.log(xs[i] / xs[i - 1]) for i in range(1, len(xs)) if xs[i - 1] > 0]
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * MINUTES_PER_YEAR)


def failed_new_extreme(bars: Sequence[Bar], direction: str) -> bool:
    """'Pushes sold into': the last bar prints a new session extreme in the trade direction and closes back inside."""
    if len(bars) < 3:
        return False
    last, prior = bars[-1], bars[:-1]
    if direction == "long":
        hi = max((b.high if b.high is not None else b.close) for b in prior)
        return (last.high or last.close) > hi and last.close < hi
    lo = min((b.low if b.low is not None else b.close) for b in prior)
    return (last.low or last.close) < lo and last.close > lo


def volume_taper(bars: Sequence[Bar], entry_bar_time: datetime, *, n: int, frac: float) -> bool:
    """The last n bars' total volume < frac × the n bars ending at entry (the push has lost its fuel)."""
    idx = [i for i, b in enumerate(bars) if b.start <= entry_bar_time]
    if not idx:
        return False
    i_entry = idx[-1]
    if i_entry < n - 1 or len(bars) - 1 - i_entry < n:
        return False
    at_entry = sum(b.volume or 0.0 for b in bars[i_entry - n + 1:i_entry + 1])
    recent = sum(b.volume or 0.0 for b in bars[-n:])
    return at_entry > 0 and recent < frac * at_entry
