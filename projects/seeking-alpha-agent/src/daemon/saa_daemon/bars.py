"""1-minute bar book built from DXLink Candle events.

dxfeed sends the forming bar many times (same `time`, changing OHLCV) and, on subscribe, the day's
history as a snapshot. The book keeps the latest state per (symbol, bar start), marks a bar complete
once the clock is past its end (or a later bar for the symbol has arrived), and hands dirty rows to the
store in batches. Pure Python; tested in tests/test_daemon_bars.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .events import CandleEvt
from .store import BarRow


@dataclass(slots=True)
class Bar:
    symbol: str
    start: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    vwap: float | None = None
    count: int | None = None
    complete: bool = False
    updates: int = 0

    def row(self) -> BarRow:
        return BarRow(self.symbol, self.start, self.open, self.high, self.low, self.close, self.volume, self.vwap, self.count, self.complete)


@dataclass
class BarBook:
    bars: dict[tuple[str, datetime], Bar] = field(default_factory=dict)
    dirty: set[tuple[str, datetime]] = field(default_factory=set)
    events: int = 0
    rejected: int = 0
    latest_start: dict[str, datetime] = field(default_factory=dict)

    def on_candle(self, e: CandleEvt) -> Bar | None:
        if e.time_ms <= 0 or e.close is None and e.open is None:
            self.rejected += 1
            return None
        start = datetime.fromtimestamp(e.time_ms / 1000.0, tz=timezone.utc)
        if start.second or start.microsecond:  # not a minute boundary → not a 1m candle we asked for
            self.rejected += 1
            return None
        key = (e.symbol, start)
        bar = self.bars.get(key)
        if bar is None:
            bar = Bar(e.symbol, start)
            self.bars[key] = bar
        # snapshots may replay older bars after newer ones; never let an old replay "un-complete" a bar
        bar.open, bar.high, bar.low, bar.close = e.open, e.high, e.low, e.close
        bar.volume, bar.vwap, bar.count = e.volume, e.vwap, e.count
        bar.updates += 1
        self.events += 1
        self.dirty.add(key)
        prev = self.latest_start.get(e.symbol)
        if prev is None or start > prev:
            self.latest_start[e.symbol] = start
            if prev is not None:
                self._complete(e.symbol, prev)
        return bar

    def _complete(self, symbol: str, start: datetime) -> None:
        b = self.bars.get((symbol, start))
        if b is not None and not b.complete:
            b.complete = True
            self.dirty.add((symbol, start))

    def complete_before(self, now: datetime) -> int:
        """Mark every bar whose minute has ended (start + 60 s <= now) as complete. Returns how many flipped."""
        n = 0
        cutoff = now - timedelta(minutes=1)
        for key, b in self.bars.items():
            if not b.complete and b.start <= cutoff:
                b.complete = True
                self.dirty.add(key)
                n += 1
        return n

    def take_dirty(self) -> list[BarRow]:
        rows = [self.bars[k].row() for k in sorted(self.dirty, key=lambda k: (k[0], k[1]))]
        self.dirty.clear()
        return rows

    def count(self, symbol: str, start: datetime, end: datetime, *, complete_only: bool = True) -> int:
        return sum(1 for (s, t), b in self.bars.items() if s == symbol and start <= t < end and (b.complete or not complete_only))

    def gaps(self, symbol: str, start: datetime, end: datetime) -> list[datetime]:
        have = {t for (s, t) in self.bars if s == symbol}
        out = []
        t = start
        while t < end:
            if t not in have:
                out.append(t)
            t += timedelta(minutes=1)
        return out

    def last_close(self, symbol: str) -> float | None:
        st = self.latest_start.get(symbol)
        if st is None:
            return None
        b = self.bars.get((symbol, st))
        return None if b is None else b.close

    def symbols(self) -> list[str]:
        return sorted({s for (s, _) in self.bars})
