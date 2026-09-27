"""The session orchestrator.

One `Daemon.run_session()` = one trading day: log in, discover chains, stream, snapshot every 5 minutes,
poll halts and VIX, heartbeat at 9:25, EOD report at close+20, stop at close+25. Every subsystem runs
under `supervise()`, which catches, logs, counts and restarts — the acceptance criterion is *zero unhandled
exceptions*, and the EOD report prints the caught ones so nothing is hidden.

All I/O components are injectable (clock, store, http, brokerage, feed, mirror, notifier) so the whole
loop runs against fakes and a virtual clock in tests/test_daemon_session_sim.py.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from . import __version__
from .bars import BarBook
from .chains import ChainPlan, OptionBook, build_snapshot, from_sdk_nested, plan_chain
from .clock import Clock, Schedule, align_up, et, is_trading_day
from .config import Settings
from .events import CandleEvt, Evt, GreeksEvt, ProfileEvt, QuoteEvt, SummaryEvt, TradeEvt
from .feed import Feed, FeedPlan
from .halts import fetch_halts, halt_from_profile
from .http import HttpClient
from .mirror import MirrorError, SupabaseMirror
from .reports import eod_text, heartbeat_text
from .store import Store, iso
from .telegram import Notifier
from .vix import fetch_vix_term

log = logging.getLogger("saa.daemon")


@dataclass
class RunResult:
    run_id: str
    trade_date: date
    ok: bool
    stats: dict[str, Any]
    unhandled: int
    errors: dict[str, int] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)


class Daemon:
    def __init__(self, settings: Settings, *, clock: Clock | None = None, store: Store | None = None,
                 http: HttpClient | None = None, brokerage: Any = None, feed_factory: Callable[[Any], Feed] | None = None,
                 mirror: SupabaseMirror | None = None, notifier: Notifier | None = None, mode: str = "session",
                 trade_date: date | None = None, force: bool = False, host: str | None = None):
        self.settings = settings
        self.clock = clock or Clock()
        self.mode = mode
        self.force = force
        self.host = host or socket.gethostname().split(".")[0]
        self._store = store
        self._http = http
        self._brokerage = brokerage
        self._feed_factory = feed_factory
        self._mirror = mirror
        self._notifier = notifier
        self._trade_date = trade_date
        # runtime
        self.bars = BarBook()
        self.options = OptionBook()
        self.plans: dict[str, ChainPlan] = {}
        self.spots: dict[str, float] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, int] = {}
        self.first_error: dict[str, str] = {}
        self.unhandled = 0
        self.feed_events = 0
        self.feed_reconnects = 0
        self.stop = asyncio.Event()
        self.stopping = False
        self.index_symbols: list[str] = list(settings.index_symbols)
        self.single_names: list[str] = []
        self.econ_today: list[str] = []
        self.vix_first: dict[str, Any] | None = None
        self.vix_last: dict[str, Any] | None = None
        self.gamma_open: dict[str, Any] | None = None
        self.gamma_last: dict[str, Any] | None = None
        self.snapshot_ticks_done = 0
        self.sched: Schedule | None = None
        self.run_id = ""
        self.started_at: datetime | None = None
        self.feed: Feed | None = None
        self.feed_plan = FeedPlan()

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.clock.now()

    def _record_error(self, task: str, exc: BaseException) -> None:
        self.errors[task] = self.errors.get(task, 0) + 1
        msg = f"{type(exc).__name__}: {str(exc)[:300]}"
        self.first_error.setdefault(task, msg)
        log.error("[%s] %s\n%s", task, msg, "".join(traceback.format_exception(exc)).rstrip()[-1500:])
        try:
            self.store.log_event(self.now(), "ERROR", task, msg)
        except Exception:  # noqa: BLE001
            pass

    async def supervise(self, name: str, fn: Callable[[], Awaitable[None]], *, restart: bool = True, max_backoff: float = 60.0) -> None:
        """Run `fn` forever (or once); exceptions are logged, counted and the coroutine restarted with backoff."""
        attempt = 0
        while not self.stopping:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._record_error(name, e)
                if not restart or self.stopping:
                    return
                attempt += 1
                delay = min(max_backoff, 2.0 ** min(attempt, 6))
                log.warning("[%s] restarting in %.0fs (attempt %d)", name, delay, attempt)
                await self.clock.sleep(delay)

    async def every(self, name: str, seconds: float, fn: Callable[[], Awaitable[None]], *, align_minutes: int | None = None) -> None:
        """Periodic task; each tick is individually guarded so one failure never stops the schedule."""
        while not self.stopping:
            if align_minutes:
                await self.clock.sleep_until(align_up(self.now() + timedelta(seconds=1), align_minutes))
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._record_error(name, e)
            if not align_minutes:
                await self.clock.sleep(seconds)

    # ------------------------------------------------------------- components
    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.settings.state_dir / "saa.sqlite")
        return self._store

    async def _components(self) -> None:
        if self._http is None:
            from .http import Httpx2Client
            self._http = Httpx2Client()
        if self._mirror is None:
            self._mirror = SupabaseMirror(self.settings, self.store, self._http)
        if self._notifier is None:
            self._notifier = Notifier(self.settings, self._mirror, self._http)

    @property
    def mirror(self) -> SupabaseMirror:
        assert self._mirror is not None
        return self._mirror

    @property
    def notifier(self) -> Notifier:
        assert self._notifier is not None
        return self._notifier

    # ------------------------------------------------------------- event path
    def on_event(self, e: Evt) -> None:
        self.feed_events += 1
        if isinstance(e, CandleEvt):
            self.bars.on_candle(e)
        elif isinstance(e, QuoteEvt):
            if e.symbol in self.feed_plan.underlyings:
                q = self.quotes.setdefault(e.symbol, {})
                q.update(bid=e.bid, ask=e.ask, bid_size=e.bid_size, ask_size=e.ask_size, ts=e.time_ms)
                if e.bid and e.ask:
                    self.spots[e.symbol] = (e.bid + e.ask) / 2.0
            else:
                self.options.on_quote(e)
        elif isinstance(e, GreeksEvt):
            self.options.on_greeks(e)
        elif isinstance(e, SummaryEvt):
            if e.symbol in self.feed_plan.underlyings:
                q = self.quotes.setdefault(e.symbol, {})
                q.update(day_open=e.day_open, prev_close=e.prev_close)
            else:
                self.options.on_summary(e)
        elif isinstance(e, TradeEvt):
            if e.symbol in self.feed_plan.underlyings:
                q = self.quotes.setdefault(e.symbol, {})
                q.update(last=e.price, day_volume=e.day_volume)
                if e.price and e.symbol not in self.spots:
                    self.spots[e.symbol] = e.price
            else:
                self.options.on_trade(e)
        elif isinstance(e, ProfileEvt):
            q = self.quotes.setdefault(e.symbol, {})
            q.update(trading_status=e.trading_status)
            h = halt_from_profile(e)
            if h:
                self.store.upsert_halts([h])

    # -------------------------------------------------------------- universe
    async def load_universe(self) -> None:
        s = self.settings
        idx = list(s.index_symbols)
        if self.mirror.enabled and not s.index_symbols_explicit:
            # saa.settings.index_symbols is the single source of truth unless .env sets SAA_INDEX_SYMBOLS (D12)
            try:
                db_idx = await self.mirror.get_setting("index_symbols")
                if db_idx:
                    idx = [x.strip().upper() for x in db_idx.split(",") if x.strip()] or idx
            except MirrorError as e:
                log.warning("index_symbols setting unavailable (%s); using %s", e, idx)
        names: list[str] = [x for x in s.extra_symbols if x not in idx]
        if self.mirror.enabled:
            try:
                for sym in await self.mirror.active_symbols():
                    if sym not in idx and sym not in names:
                        names.append(sym)
            except MirrorError as e:
                log.warning("active symbols unavailable (%s)", e)
            try:
                day = await self.mirror.calendar_day(self.sched.trade_date.isoformat() if self.sched else None)
                self.econ_today = [f"{ev.get('event')} {ev.get('time_et') or ''}".strip() for ev in (day.get("econ") or []) if isinstance(ev, dict)]
            except MirrorError:
                pass
        self.index_symbols = idx
        self.single_names = names[: s.max_single_names]
        if len(names) > s.max_single_names:
            log.warning("universe capped: %d single names requested, keeping %d", len(names), s.max_single_names)

    async def discover_chains(self, symbols: list[str]) -> FeedPlan:
        """Spots → nested chains → ChainPlans → the feed delta for these symbols."""
        s = self.settings
        delta = FeedPlan(candle_start=self.sched.open if self.sched else None)
        if self._brokerage is None or self._brokerage.data is None:
            return delta
        try:
            self.spots.update(await self._brokerage.spot_prices(symbols))
        except Exception as e:  # noqa: BLE001
            self._record_error("spots", e)
        today = self.sched.trade_date if self.sched else et(self.now()).date()
        now_et = et(self.now()).time()
        for sym in symbols:
            spot = self.spots.get(sym)
            if not spot:
                log.warning("no spot for %s — chain skipped this round", sym)
                continue
            try:
                nested = await self._brokerage.nested_chain(sym)
            except Exception as e:  # noqa: BLE001
                self._record_error("chain", e)
                continue
            if nested is None:
                log.warning("no option chain for %s", sym)
                continue
            is_idx = sym in self.index_symbols
            plan = plan_chain(sym, from_sdk_nested(nested), spot, today=today, now_et=now_et,
                              n_exp=s.expirations_index if is_idx else s.expirations_single,
                              window_pct=s.strike_window_pct_index if is_idx else s.strike_window_pct_single,
                              max_per_side=s.max_strikes_per_side)
            if not plan.expirations:
                log.warning("no live expirations for %s", sym)
                continue
            self.plans[sym] = plan
            delta.options |= set(plan.symbols())
            delta.underlyings.add(sym)
            delta.candles.add(sym)
        return delta

    # ------------------------------------------------------------- snapshots
    async def snapshot_all(self, ts: datetime) -> None:
        for sym, plan in list(self.plans.items()):
            snap = build_snapshot(plan, self.options, self.spots.get(sym), ts)
            self.store.insert_snapshot(ts, sym, snap["spot"], snap["expirations"], snap["summary"], snap["gamma"])
            if sym == (self.index_symbols[0] if self.index_symbols else "SPY"):
                self.gamma_last = snap["gamma"]
                if self.gamma_open is None and self.sched and ts >= self.sched.open:
                    self.gamma_open = snap["gamma"]
        self.snapshot_ticks_done += 1
        log.info("snapshot tick %d at %s ET: %d underlyings, %d option states", self.snapshot_ticks_done, et(ts).strftime("%H:%M"),
                 len(self.plans), len(self.options.state))

    async def snapshot_scheduler(self) -> None:
        assert self.sched is not None
        for tick in self.sched.snapshot_ticks(self.settings.snapshot_minutes):
            if tick < self.now() - timedelta(minutes=1):
                continue
            await self.clock.sleep_until(tick)
            if self.stopping:
                return
            try:
                await self.snapshot_all(tick)
            except Exception as e:  # noqa: BLE001
                self._record_error("snapshot", e)

    # ------------------------------------------------------------- periodic
    async def flush_bars(self) -> None:
        self.bars.complete_before(self.now())
        rows = self.bars.take_dirty()
        if rows:
            self.store.upsert_bars(rows, self.now())
        for sym, q in self.quotes.items():
            if q.get("bid") is not None or q.get("last") is not None:
                self.store.upsert_quote(sym, self.now(), **{k: v for k, v in q.items() if k != "ts"})

    async def mirror_round(self) -> None:
        await self.mirror.flush_all()

    async def pulse(self) -> None:
        """Machine heartbeat (every 60 s): run row + settings.daemon_last_seen in Supabase, run row in SQLite."""
        stats = self.stats_snapshot()
        self.store.upsert_run(self.run_id, self.sched.trade_date.isoformat() if self.sched else "", self.started_at or self.now(),
                              "running", stats)
        if self.mirror.enabled:
            try:
                await self.mirror.rpc("saa_daemon_run", {"p_run_id": self.run_id, "p_patch": {"stats": stats, "errors": self._errors_payload()}})
            except MirrorError as e:
                log.debug("pulse mirror failed: %s", e)

    async def poll_halts(self) -> None:
        if self._http is None:
            return
        rows = await fetch_halts(self._http)
        if rows:
            self.store.upsert_halts(rows)

    async def poll_vix(self) -> None:
        if self._http is None:
            return
        term = await fetch_vix_term(self._http, self.now())
        if any(term.get(k) is not None for k in ("vix", "vix1d", "vix9d", "vix3m")):
            self.store.insert_vix(self.now(), term)
            if self.vix_first is None:
                self.vix_first = term
            self.vix_last = term

    async def refresh_watch(self) -> None:
        if not self.mirror.enabled or self.feed is None:
            return
        try:
            active = await self.mirror.active_symbols()
        except MirrorError:
            return
        new = [s for s in active if s not in self.index_symbols and s not in self.single_names]
        room = self.settings.max_single_names - len(self.single_names)
        new = new[:max(0, room)]
        if not new:
            return
        self.single_names.extend(new)
        delta = await self.discover_chains(new)
        if delta.size:
            self.feed_plan.merge(delta)
            await self.feed.add(delta)
            log.info("watchlist grew: +%s", " ".join(new))

    # ------------------------------------------------------------- messaging
    def _heartbeat_ctx(self) -> dict[str, Any]:
        b = self._brokerage
        spy = self.index_symbols[0] if self.index_symbols else "SPY"
        plan_spy = self.plans.get(spy)
        gamma = None
        if plan_spy is not None:
            gamma = build_snapshot(plan_spy, self.options, self.spots.get(spy), self.now())["gamma"]
        n_idx = sum(1 for s in self.plans if s in self.index_symbols)
        n_single = len(self.plans) - n_idx
        return {
            "trade_date": self.sched.trade_date if self.sched else et(self.now()).date(), "version": __version__, "host": self.host,
            "late": bool(self.sched and self.now() > self.sched.heartbeat + timedelta(minutes=2)),
            "broker": vars(b.broker_info) if b is not None else {}, "data": vars(b.data_info) if b is not None else {},
            "index_symbols": self.index_symbols, "single_names": self.single_names,
            "chains": {"n_index": n_idx, "n_single": n_single, "exp_index": self.settings.expirations_index,
                       "exp_single": self.settings.expirations_single, "n_options": len(self.feed_plan.options)},
            "vix": self.vix_last, "gamma": gamma, "halts": len(self.store.halts_since(self.sched.open - timedelta(hours=6))) if self.sched else 0,
            "econ": self.econ_today, "mirror": self.mirror.enabled,
        }

    async def send_heartbeat(self) -> None:
        await self.notifier.send("system", heartbeat_text(self._heartbeat_ctx()), self.now())
        self.mirror.queue("saa_log_run", {"p_job": "daemon:heartbeat", "p_ok": True, "p_detail": {"run_id": self.run_id, "universe": self.index_symbols + self.single_names}}, self.now())

    def _errors_payload(self) -> list[dict[str, Any]]:
        return [{"task": k, "count": v, "first": self.first_error.get(k)} for k, v in sorted(self.errors.items())]

    def stats_snapshot(self) -> dict[str, Any]:
        sched = self.sched
        bars: dict[str, Any] = {}
        if sched is not None:
            end = min(self.now(), sched.close)
            for sym in sorted(self.feed_plan.candles):
                bars[sym] = self.store.bar_stats(sym, sched.open, end) if end > sched.open else {"complete": 0, "expected": 0, "missing": 0}
        snaps = self.store.snapshot_counts(sched.heartbeat - timedelta(minutes=1), sched.shutdown) if sched else {}
        return {
            "trade_date": sched.trade_date.isoformat() if sched else None, "mode": self.mode, "version": __version__, "host": self.host,
            "started": et(self.started_at).strftime("%H:%M") if self.started_at else None, "now": iso(self.now()),
            "unhandled": self.unhandled, "errors": dict(self.errors),
            "bars": bars, "snapshots": snaps, "snapshots_expected": len(sched.snapshot_ticks(self.settings.snapshot_minutes)) if sched else None,
            "snapshot_ticks_done": self.snapshot_ticks_done, "n_options": len(self.feed_plan.options), "universe": self.index_symbols + self.single_names,
            "feed": {"events": self.feed_events, "reconnects": self.feed_reconnects, "by_kind": getattr(self.feed, "by_kind", {})},
            "gamma_open": self.gamma_open, "gamma_close": self.gamma_last, "vix_open": self.vix_first, "vix_close": self.vix_last,
            "halts": self.store.halts_since(sched.open - timedelta(hours=6)) if sched else [],
            "mirror": self.mirror.status() if self._mirror is not None else {"enabled": False}, "telegram": list(self.notifier.sent) if self._notifier else [],
            "broker": vars(self._brokerage.broker_info) if self._brokerage is not None else {}, "data": vars(self._brokerage.data_info) if self._brokerage is not None else {},
        }

    async def send_eod(self) -> dict[str, Any]:
        stats = self.stats_snapshot()
        stats["ended"] = et(self.now()).strftime("%H:%M")
        await self.notifier.send("system", eod_text(stats), self.now())
        return stats

    # ----------------------------------------------------------------- main
    async def run_session(self) -> RunResult:
        loop = asyncio.get_running_loop()

        def on_loop_exception(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            self.unhandled += 1
            exc = context.get("exception")
            log.critical("UNHANDLED asyncio exception: %s", context.get("message"), exc_info=exc)

        loop.set_exception_handler(on_loop_exception)
        await self._components()
        today = self._trade_date or et(self.now()).date()
        self.sched = Schedule.for_date(today)
        sched = self.sched
        self.started_at = self.now()
        self.run_id = f"{today.isoformat()}-{et(self.started_at):%H%M%S}-{self.mode}"
        if not is_trading_day(today) and not self.force:
            log.info("%s is not a trading day — nothing to do (use --force to run anyway)", today)
            return RunResult(self.run_id, today, True, {"skipped": "non-trading day"}, 0)
        if self.now() >= sched.shutdown and not self.force:
            log.info("session for %s is over (now %s ET) — nothing to do", today, et(self.now()).strftime("%H:%M"))
            return RunResult(self.run_id, today, True, {"skipped": "session over"}, 0)

        log.info("run %s starting (mode=%s, host=%s, v%s)", self.run_id, self.mode, self.host, __version__)
        self.store.upsert_run(self.run_id, today.isoformat(), self.started_at, "running", {})
        self.mirror.queue("saa_daemon_run", {"p_run_id": self.run_id, "p_patch": {"trade_date": today.isoformat(), "started_at": iso(self.started_at),
                                                                                   "host": self.host, "version": __version__, "mode": self.mode}}, self.now())
        self.mirror.queue("saa_log_run", {"p_job": "daemon:start", "p_ok": True, "p_detail": {"run_id": self.run_id, "mode": self.mode, "host": self.host, "version": __version__}}, self.now())
        await self.mirror.flush_queue()

        # 1. wait for prep time
        if self.now() < sched.prep:
            log.info("waiting until prep %s ET", et(sched.prep).strftime("%H:%M"))
            await self.clock.sleep_until(sched.prep)

        # 2. log in (retry until the data session is up or the day is over)
        if self._brokerage is None:
            from .broker import Brokerage
            self._brokerage = Brokerage(self.settings)
        attempt = 0
        while not self.stopping:
            try:
                await self._brokerage.open()
                if self._brokerage.data is not None:
                    break
                raise RuntimeError(f"data login failed: {self._brokerage.data_info.error}")
            except Exception as e:  # noqa: BLE001
                self._record_error("login", e)
                attempt += 1
                if self.now() >= sched.report:
                    break
                await self.clock.sleep(min(300.0, 30.0 * attempt))

        # 3. universe + chains + feed
        await self.load_universe()
        delta = await self.discover_chains(self.index_symbols + self.single_names)
        self.feed_plan = FeedPlan(set(self.index_symbols + self.single_names), set(self.index_symbols + self.single_names), set(), sched.open)
        self.feed_plan.merge(delta)
        if self._feed_factory is not None:
            self.feed = self._feed_factory(self._brokerage)
        elif self._brokerage.data is not None:
            from .feed import DXLinkFeed, Recorder
            rec = None
            if self.settings.record_events:
                rec = Recorder(self.settings.state_dir / "recordings" / f"{self.run_id}.jsonl", underlying_symbols=set(self.feed_plan.underlyings))
            self.feed = DXLinkFeed(self._brokerage.data, recorder=rec, now_ms=lambda: int(self.now().timestamp() * 1000))

        feed_runs = 0

        async def run_feed() -> None:
            nonlocal feed_runs
            assert self.feed is not None
            feed_runs += 1
            if feed_runs > 1:
                self.feed_reconnects += 1
            await self.feed.run(self.feed_plan, self.on_event, self.stop)

        tasks: list[asyncio.Task[Any]] = []
        if self.feed is not None:
            tasks.append(asyncio.create_task(self.supervise("feed", run_feed), name="feed"))
        else:
            log.error("no data session — running without market data (heartbeat/VIX/halts only)")
        tasks += [
            asyncio.create_task(self.every("bars", 5.0, self.flush_bars), name="bars"),
            asyncio.create_task(self.every("mirror", 10.0, self.mirror_round), name="mirror"),
            asyncio.create_task(self.every("pulse", 60.0, self.pulse), name="pulse"),
            asyncio.create_task(self.every("halts", float(self.settings.halts_poll_seconds), self.poll_halts), name="halts"),
            asyncio.create_task(self.every("vix", 0, self.poll_vix, align_minutes=self.settings.vix_poll_minutes), name="vix"),
            asyncio.create_task(self.every("watch", 300.0, self.refresh_watch), name="watch"),
            asyncio.create_task(self.supervise("snapshots", self.snapshot_scheduler, restart=False), name="snapshots"),
        ]
        # first VIX read right away so the heartbeat has it
        try:
            await self.poll_vix()
        except Exception as e:  # noqa: BLE001
            self._record_error("vix", e)

        # 4. heartbeat
        await self.clock.sleep_until(sched.heartbeat)
        try:
            await self.send_heartbeat()
        except Exception as e:  # noqa: BLE001
            self._record_error("heartbeat", e)

        # 5. run to the report time
        await self.clock.sleep_until(sched.report)
        try:
            await self.flush_bars()
        except Exception as e:  # noqa: BLE001
            self._record_error("bars", e)
        try:
            stats = await self.send_eod()
        except Exception as e:  # noqa: BLE001
            self._record_error("eod", e)
            stats = self.stats_snapshot()

        # 6. shutdown
        await self.clock.sleep_until(min(sched.shutdown, self.now() + timedelta(minutes=5)))
        self.stopping = True
        self.stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.flush_bars()
        except Exception as e:  # noqa: BLE001
            self._record_error("bars", e)
        ok = self.unhandled == 0
        stats = self.stats_snapshot()
        stats["ended"] = et(self.now()).strftime("%H:%M")
        self.store.upsert_run(self.run_id, today.isoformat(), self.started_at, "done" if ok else "failed", stats, ended_at=self.now())
        self.mirror.queue("saa_daemon_run", {"p_run_id": self.run_id, "p_patch": {"status": "done" if ok else "failed", "ended_at": iso(self.now()),
                                                                                   "stats": stats, "errors": self._errors_payload()}}, self.now())
        self.mirror.queue("saa_log_run", {"p_job": "daemon:session", "p_ok": ok, "p_detail": {
            "run_id": self.run_id, "unhandled": self.unhandled, "errors": self.errors,
            "bars": {k: {"complete": v.get("complete"), "expected": v.get("expected"), "missing": v.get("missing")} for k, v in stats["bars"].items()},
            "snapshots": stats["snapshots"], "snapshots_expected": stats["snapshots_expected"], "n_options": stats["n_options"],
            "feed": stats["feed"], "mirror": stats["mirror"]}}, self.now())
        for _ in range(3):
            try:
                await self.mirror.flush_all()
                if self.store.queue_size() == 0:
                    break
            except Exception as e:  # noqa: BLE001
                self._record_error("mirror", e)
            await self.clock.sleep(5.0)
        if self._brokerage is not None:
            try:
                await self._brokerage.close()
            except Exception as e:  # noqa: BLE001
                self._record_error("close", e)
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        log.info("run %s finished: unhandled=%d errors=%s", self.run_id, self.unhandled, self.errors or "none")
        return RunResult(self.run_id, today, ok, stats, self.unhandled, dict(self.errors), list(self.notifier.sent))
