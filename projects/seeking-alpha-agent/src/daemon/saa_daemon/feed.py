"""DXLink market-data feed (tastytrade SDK) behind a small interface the daemon and the tests share.

`DXLinkFeed.run()` opens the streamer on the production data session, subscribes the plan (chunked, so a
big option list never trips the "subscription message too long" limit), fans every event into the
daemon's synchronous `on_event` callback as a plain record (events.py), and records underlying-level
events to JSONL for replay. A websocket drop raises out of `run()`; the daemon's supervisor re-runs it
with backoff and the plan is re-subscribed (candles re-request the day from the open → idempotent).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .events import CandleEvt, Evt, ProfileEvt, QuoteEvt, SummaryEvt, TradeEvt, from_sdk, to_record
from .log import quiet_sdk_loggers

log = logging.getLogger("saa.feed")

OPTION_CHUNK = 150
UNDERLYING_EVENTS = (CandleEvt, QuoteEvt, TradeEvt, ProfileEvt, SummaryEvt)


@dataclass
class FeedPlan:
    underlyings: set[str] = field(default_factory=set)   # Quote + Trade + Profile + Summary
    candles: set[str] = field(default_factory=set)       # 1-minute candles from `candle_start`
    options: set[str] = field(default_factory=set)       # Quote + Greeks + Summary + Trade (streamer symbols)
    candle_start: datetime | None = None

    def diff(self, other: "FeedPlan") -> "FeedPlan":
        """Symbols in `other` not yet in self."""
        return FeedPlan(other.underlyings - self.underlyings, other.candles - self.candles, other.options - self.options, other.candle_start)

    def merge(self, other: "FeedPlan") -> None:
        self.underlyings |= other.underlyings
        self.candles |= other.candles
        self.options |= other.options
        if self.candle_start is None:
            self.candle_start = other.candle_start

    @property
    def size(self) -> int:
        return len(self.underlyings) + len(self.candles) + len(self.options)


class Feed(Protocol):
    async def run(self, plan: FeedPlan, on_event: Callable[[Evt], None], stop: asyncio.Event) -> None: ...
    async def add(self, delta: FeedPlan) -> None: ...


class Recorder:
    """JSONL recorder of underlying-level events (option quotes are too many; the 5-minute snapshots
    capture option state). Replay reads these plus the snapshot table."""

    def __init__(self, path: Path, *, underlying_symbols: set[str]):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("a", encoding="utf-8")
        self.symbols = underlying_symbols
        self.n = 0

    def maybe(self, e: Evt, recv_ms: int) -> None:
        if isinstance(e, UNDERLYING_EVENTS) and e.symbol in self.symbols:
            rec = to_record(e)
            rec["recv_ms"] = recv_ms
            self._f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self.n += 1
            if self.n % 500 == 0:
                self._f.flush()

    def write(self, kind: str, rec: dict[str, Any], recv_ms: int) -> None:
        """Engine-side records (Meta / Plan / OptMarks / Tick) — written in the same stream so replay sees the live order."""
        row = {"kind": kind, **rec, "recv_ms": recv_ms}
        self._f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        self.n += 1
        self._f.flush()

    def close(self) -> None:
        try:
            self._f.flush()
            self._f.close()
        except Exception:  # noqa: BLE001
            pass


class DXLinkFeed:
    def __init__(self, session: Any, *, recorder: Recorder | None = None, now_ms: Callable[[], int] | None = None):
        self.session = session
        self.recorder = recorder
        self._now_ms = now_ms or (lambda: int(datetime.now().timestamp() * 1000))
        self._streamer: Any = None
        self._plan = FeedPlan()
        self._pending: list[FeedPlan] = []
        self.events = 0
        self.by_kind: dict[str, int] = {}
        self.reconnects = 0

    async def run(self, plan: FeedPlan, on_event: Callable[[Evt], None], stop: asyncio.Event) -> None:
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Candle, Greeks, Profile, Quote, Summary, Trade

        quiet_sdk_loggers()
        self._plan = FeedPlan(set(plan.underlyings), set(plan.candles), set(plan.options), plan.candle_start)
        async with DXLinkStreamer(self.session) as st:
            self._streamer = st
            await self._subscribe(st, self._plan)
            for delta in self._pending:
                await self._subscribe(st, delta)
                self._plan.merge(delta)
            self._pending.clear()

            async def listen(cls: Any) -> None:
                async for ev in st.listen(cls):
                    rec = from_sdk(ev)
                    if rec is None:
                        continue
                    self.events += 1
                    self.by_kind[rec.kind] = self.by_kind.get(rec.kind, 0) + 1
                    if self.recorder is not None:
                        self.recorder.maybe(rec, self._now_ms())
                    try:
                        on_event(rec)
                    except Exception as e:  # noqa: BLE001 - a handler bug must not kill the feed
                        log.exception("event handler failed on %s %s: %s", rec.kind, rec.symbol, e)

            tasks = [asyncio.create_task(listen(c), name=f"listen-{c.__name__}") for c in (Quote, Greeks, Summary, Trade, Candle, Profile)]
            stopper = asyncio.create_task(stop.wait(), name="feed-stop")
            try:
                done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is not stopper and t.exception() is not None:
                        raise t.exception()  # type: ignore[misc]
            finally:
                for t in [*tasks, stopper]:
                    t.cancel()
                await asyncio.gather(*tasks, stopper, return_exceptions=True)
                self._streamer = None

    async def _subscribe(self, st: Any, plan: FeedPlan) -> None:
        from tastytrade.dxfeed import Greeks, Profile, Quote, Summary, Trade

        if plan.underlyings:
            und = sorted(plan.underlyings)
            await st.subscribe(Quote, und, refresh_interval=0.5)
            await st.subscribe(Trade, und, refresh_interval=0.5)
            await st.subscribe(Summary, und, refresh_interval=5.0)
            await st.subscribe(Profile, und, refresh_interval=5.0)
        if plan.candles:
            await st.subscribe_candle(sorted(plan.candles), "1m", start_time=plan.candle_start, extended_trading_hours=False,
                                      refresh_interval=1.0)
        if plan.options:
            opts = sorted(plan.options)
            for i in range(0, len(opts), OPTION_CHUNK):
                chunk = opts[i:i + OPTION_CHUNK]
                await st.subscribe(Quote, chunk, refresh_interval=2.0)
                await st.subscribe(Greeks, chunk, refresh_interval=2.0)
                await st.subscribe(Summary, chunk, refresh_interval=5.0)
                await st.subscribe(Trade, chunk, refresh_interval=5.0)
                await asyncio.sleep(0.05)
        log.info("subscribed: %d underlyings, %d candle symbols, %d options", len(plan.underlyings), len(plan.candles), len(plan.options))

    async def add(self, delta: FeedPlan) -> None:
        """Subscribe more symbols while running (new watchlist names). Queued if the socket is reconnecting."""
        delta = self._plan.diff(delta)
        if delta.size == 0:
            return
        if self._streamer is None:
            self._pending.append(delta)
            return
        await self._subscribe(self._streamer, delta)
        self._plan.merge(delta)
        if self.recorder is not None:
            self.recorder.symbols |= delta.underlyings | delta.candles
