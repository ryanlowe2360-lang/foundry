"""Time: Eastern-time helpers, the trading calendar, the session schedule, and an injectable clock
so the whole daemon can run against a virtual clock in tests (see tests/test_daemon_session_sim.py).
"""
from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# NYSE holidays / early closes, generated from pandas_market_calendars 5.4.0 on 2026-09-27 and kept
# here so the daemon needs no calendar library at runtime. Refresh each December (maintenance plan).
NYSE_HOLIDAYS: frozenset[date] = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
})
NYSE_EARLY_CLOSE: dict[date, time] = {
    date(2026, 11, 27): time(13, 0), date(2026, 12, 24): time(13, 0), date(2027, 11, 26): time(13, 0),
}

OPEN_T = time(9, 30)
CLOSE_T = time(16, 0)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def et(dt: datetime) -> datetime:
    """Any aware datetime → Eastern."""
    return dt.astimezone(ET)


def et_dt(d: date, t: time) -> datetime:
    """Eastern wall-clock date+time → aware datetime (UTC)."""
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def align_up(dt: datetime, minutes: int) -> datetime:
    """Smallest wall-clock (ET) instant >= dt whose minute is a multiple of `minutes`, seconds = 0."""
    local = et(dt)
    base = local.replace(second=0, microsecond=0)
    rem = base.minute % minutes
    if rem == 0 and base == local:
        return local.astimezone(UTC)
    base = base + timedelta(minutes=minutes - rem)
    return base.astimezone(UTC)


@dataclass(frozen=True)
class Schedule:
    """The session's fixed points (aware UTC datetimes) for one trading date."""

    trade_date: date
    prep: datetime        # 09:20 — log in, discover chains, subscribe
    heartbeat: datetime   # 09:25 — Telegram heartbeat
    open: datetime        # 09:30
    close: datetime       # 16:00 (13:00 on early-close days)
    report: datetime      # close + 20 min — Telegram EOD data report
    shutdown: datetime    # close + 25 min
    early_close: bool

    @classmethod
    def for_date(cls, d: date) -> "Schedule":
        close_t = NYSE_EARLY_CLOSE.get(d, CLOSE_T)
        close = et_dt(d, close_t)
        return cls(
            trade_date=d,
            prep=et_dt(d, time(9, 20)),
            heartbeat=et_dt(d, time(9, 25)),
            open=et_dt(d, OPEN_T),
            close=close,
            report=close + timedelta(minutes=20),
            shutdown=close + timedelta(minutes=25),
            early_close=close_t != CLOSE_T,
        )

    @property
    def session_minutes(self) -> int:
        return int((self.close - self.open).total_seconds() // 60)

    def in_session(self, dt: datetime) -> bool:
        return self.open <= dt < self.close

    def snapshot_ticks(self, minutes: int) -> list[datetime]:
        """Snapshot instants: one at heartbeat (pre-open baseline) then every `minutes` from the open through the close."""
        ticks = [self.heartbeat]
        t = self.open
        while t <= self.close:
            ticks.append(t)
            t += timedelta(minutes=minutes)
        return ticks


class Clock:
    """Real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, when: datetime) -> None:
        while True:
            delta = (when - self.now()).total_seconds()
            if delta <= 0:
                return
            await self.sleep(min(delta, 30.0))


class FakeClock(Clock):
    """Deterministic virtual clock: `sleep()` parks the caller until the driver advances time to its
    wake-up instant, in wake-up order. Used by the simulated-session test; real code never sees it."""

    def __init__(self, start: datetime):
        self._now = start
        self._heap: list[tuple[datetime, int, asyncio.Future]] = []
        self._seq = 0

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        await self.sleep_until(self._now + timedelta(seconds=seconds))

    async def sleep_until(self, when: datetime) -> None:
        if when <= self._now:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._seq += 1
        heapq.heappush(self._heap, (when, self._seq, fut))
        await fut

    def pending(self) -> int:
        return len(self._heap)

    async def advance(self) -> bool:
        """Advance to the earliest wake-up and release it. Returns False when nothing is waiting."""
        await asyncio.sleep(0)
        if not self._heap:
            return False
        when, _, fut = heapq.heappop(self._heap)
        if when > self._now:
            self._now = when
        if not fut.done():
            fut.set_result(None)
        await asyncio.sleep(0)
        return True

    async def run_until(self, stop: datetime, *, idle_limit: int = 50) -> None:
        """Drive time forward until `stop` (inclusive) or until no task is sleeping."""
        idle = 0
        while self._now < stop:
            if not self._heap:
                await asyncio.sleep(0)
                idle += 1
                if idle > idle_limit:
                    break
                continue
            idle = 0
            if self._heap[0][0] > stop:
                self._now = stop
                break
            await self.advance()
