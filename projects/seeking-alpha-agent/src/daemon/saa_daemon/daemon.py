"""The session orchestrator.

One `Daemon.run_session()` = one trading day: log in, discover chains, stream, snapshot every 5 minutes, poll halts and
VIX, heartbeat at 9:25, run the rules engine once a minute (M3), EOD report at close+20, stop at close+25. Every
subsystem runs under `supervise()`, which catches, logs, counts and restarts — the acceptance criterion is *zero
unhandled exceptions*, and the EOD report prints the caught ones so nothing is hidden.

All I/O components are injectable (clock, store, http, brokerage, feed, mirror, notifier) so the whole loop runs
against fakes and a virtual clock in tests/test_daemon_session_sim.py.

M3: the engine ticks 2 s into every minute on the mark digest the recorder writes (`OptMarks` + `Tick` records), so a
replay of the recording reproduces the live decisions byte for byte. Live evaluation is gated on
`feed_lag.mode == realtime`; otherwise the engine runs observe-only and records why.

M4: the engine's gate-fired open / bank / close events feed the paper **executor** (`execution/`): Telegram approval
(Approve / Skip, 3-minute timeout = Skip), limit-at-mid retry ladder in the sandbox account, fill logging, reconciliation
every 30 s, kill switch (`/halt` or the `state/HALT` file → flat within 10 s). The order path sits behind the same
real-time gate as the engine (D19). The Telegram bot is long-polled from here while the session runs.
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
from .chains import ChainPlan, build_snapshot, from_sdk_nested, plan_chain
from .clock import Clock, Schedule, align_up, et, floor_minute, is_trading_day, next_trading_day
from .config import Settings
from .engine import Engine, EngineConfig, EngineInputs, Rules
from .events import Evt, ProfileEvt
from .execution import ApprovalGate, ExecutionPolicy, Executor, KillSwitch
from .execution.telegram_bot import TelegramBot
from .feed import Feed, FeedPlan, Recorder
from .halts import fetch_halts, halt_from_profile
from .http import HttpClient
from .market import MarketState, plan_to_record
from .mirror import MirrorError, SupabaseMirror
from .reports import eod_text, heartbeat_text
from .store import Store, iso
from .telegram import Notifier
from .vix import fetch_vix_term

log = logging.getLogger("saa.daemon")

ENGINE_TICK_OFFSET_S = 2     # tick 2 s into the minute: the previous bar is complete, the first quotes of the new minute are in


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
                 trade_date: date | None = None, force: bool = False, host: str | None = None,
                 broker_factory: Callable[[Any], Awaitable[Any]] | None = None, bot: TelegramBot | None = None,
                 execution_policy: ExecutionPolicy | None = None):
        self.settings = settings
        self.clock = clock or Clock()
        self.mode = mode
        self.force = force
        self.host = host or socket.gethostname().split(".")[0]
        self._store = store
        self._http = http
        self._http_fallback: HttpClient | None = None
        self._brokerage = brokerage
        self._feed_factory = feed_factory
        self._mirror = mirror
        self._notifier = notifier
        self._trade_date = trade_date
        # runtime
        self.market = MarketState.new(on_profile=self._on_profile)
        self.errors: dict[str, int] = {}
        self.first_error: dict[str, str] = {}
        self.unhandled = 0
        self.feed_reconnects = 0
        self.stop = asyncio.Event()
        self.stopping = False
        self.index_symbols: list[str] = list(settings.index_symbols)
        self.single_names: list[str] = []
        self.econ_today: list[dict[str, Any]] = []
        self.vix_first: dict[str, Any] | None = None
        self.vix_last: dict[str, Any] | None = None
        self.gamma_open: dict[str, Any] | None = None
        self.gamma_last: dict[str, Any] | None = None
        self.gamma_by_symbol: dict[str, dict[str, Any]] = {}
        self.snapshot_ticks_done = 0
        self.sched: Schedule | None = None
        self.run_id = ""
        self.started_at: datetime | None = None
        self.feed: Feed | None = None
        self.feed_plan = FeedPlan()
        self.recorder: Recorder | None = None
        # engine (M3)
        self.engine: Engine | None = None
        self.engine_meta: dict[str, Any] = {}
        self.engine_ticks = 0
        self._decisions_persisted = 0
        # execution (M4)
        self._broker_factory = broker_factory
        self._bot = bot
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.killswitch = KillSwitch(settings.state_dir)
        self.executor: Executor | None = None
        self.bot: TelegramBot | None = None
        self.execution_off_reason: str | None = None
        self._halt_handled = False

    # ---------------------------------------------------------------- market state passthroughs (tests + reports)
    @property
    def bars(self):
        return self.market.bars

    @property
    def options(self):
        return self.market.options

    @property
    def plans(self) -> dict[str, ChainPlan]:
        return self.market.plans

    @property
    def spots(self) -> dict[str, float]:
        return self.market.spots

    @property
    def quotes(self) -> dict[str, dict[str, Any]]:
        return self.market.quotes

    @property
    def lag_samples(self):
        return self.market.lag_samples

    @property
    def feed_events(self) -> int:
        return self.market.feed_events

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.clock.now()

    def now_ms(self) -> int:
        return int(self.now().timestamp() * 1000)

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
            from .http import Httpx2Client, certifi_client
            self._http = Httpx2Client()
            self._http_fallback = certifi_client()
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
        now = self.now()
        if self.recorder is not None:
            self.recorder.maybe(e, int(now.timestamp() * 1000))
        self.market.on_event(e, now)

    def _on_profile(self, e: ProfileEvt) -> None:
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
                self.econ_today = [ev for ev in (day.get("econ") or []) if isinstance(ev, dict)]
            except MirrorError:
                pass
        self.index_symbols = idx
        self.single_names = names[: s.max_single_names]
        if len(names) > s.max_single_names:
            log.warning("universe capped: %d single names requested, keeping %d", len(names), s.max_single_names)

    @property
    def econ_lines(self) -> list[str]:
        return [f"{ev.get('event')} {ev.get('time_et') or ''}".strip() for ev in self.econ_today]

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
            self.market.underlyings.add(sym)
            if self.recorder is not None:
                self.recorder.write("Plan", plan_to_record(plan), self.now_ms())
            delta.options |= set(plan.symbols())
            delta.underlyings.add(sym)
            delta.candles.add(sym)
        return delta

    # ------------------------------------------------------------- snapshots
    async def snapshot_all(self, ts: datetime) -> None:
        for sym, plan in list(self.plans.items()):
            snap = build_snapshot(plan, self.options, self.spots.get(sym), ts)
            self.store.insert_snapshot(ts, sym, snap["spot"], snap["expirations"], snap["summary"], snap["gamma"])
            self.gamma_by_symbol[sym] = snap["gamma"]
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

    # ---------------------------------------------------------------- engine
    async def load_engine(self) -> None:
        """Build the M3 engine from saa.rules / today's checklists / account settings / the ledger history (D16–D18)."""
        assert self.sched is not None
        today = self.sched.trade_date
        rules = Rules.default()
        checklists: list[dict[str, Any]] = []
        account, k = 1000.0, 0.5
        history: list[float] = []
        cooling_off = False
        if self.mirror.enabled:
            try:
                row = await self.mirror.rules_latest()
                if row:
                    rules = Rules.from_row(row)
            except (MirrorError, ValueError) as e:
                log.warning("rules unavailable (%s); using built-in defaults", e)
            try:
                checklists = await self.mirror.checklists_today(today.isoformat())
            except MirrorError as e:
                log.warning("checklists unavailable (%s)", e)
            for key, cast, setter in (("account_size", float, "account"), ("kelly_k", float, "k")):
                try:
                    v = await self.mirror.get_setting(key)
                    if v:
                        if setter == "account":
                            account = cast(v)
                        else:
                            k = cast(v)
                except (MirrorError, ValueError) as e:
                    log.warning("setting %s unavailable (%s)", key, e)
            try:
                history = [float(r["r_result"]) for r in await self.mirror.engine_ledger(60) if r.get("r_result") is not None]
            except (MirrorError, ValueError, TypeError) as e:
                log.warning("engine ledger unavailable (%s)", e)
            try:
                co = await self.mirror.get_setting("engine_cooling_off_after")
                if co:
                    cooling_off = next_trading_day(date.fromisoformat(co[:10])) == today
            except (MirrorError, ValueError) as e:
                log.warning("cooling-off setting unavailable (%s)", e)
        else:
            co = self.store.get_kv("engine_cooling_off_after")
            if co:
                cooling_off = next_trading_day(date.fromisoformat(co[:10])) == today
        cfg = EngineConfig(trade_date=today, account=account, kelly_k=k, index_symbols=tuple(self.index_symbols), require_realtime=True)
        self.engine = Engine(cfg, rules, checklists=checklists, econ_events=self.econ_today, history_r=history, cooling_off=cooling_off)
        self.engine_meta = {"trade_date": today.isoformat(), "account": account, "kelly_k": k, "index_symbols": list(self.index_symbols),
                            "universe": self.index_symbols + self.single_names, "rules": {"version": rules.version, "params": rules.params,
                                                                                         "evidence": rules.evidence, "ignored": list(rules.ignored)},
                            "checklists": checklists, "econ": self.econ_today, "history_r": history, "cooling_off": cooling_off,
                            "require_realtime": True, "version": __version__, "run_id": self.run_id}
        if self.recorder is not None:
            self.recorder.write("Meta", self.engine_meta, self.now_ms())
        log.info("engine ready: rules v%d, account $%.0f, k=%.2f, %d checklists, %d prior trades, cooling-off=%s, %d econ events",
                 rules.version, account, k, len(checklists), len(history), cooling_off, len(self.econ_today))

    def _engine_universe(self) -> list[str]:
        return sorted(self.plans)

    async def engine_tick(self, now: datetime | None = None, *, final: bool = False) -> None:
        """One engine minute: digests → recording → engine → persistence → alerts."""
        if self.engine is None:
            return
        now = now or self.now()
        self.bars.complete_before(now)
        pct = float(self.engine.rules.shadow.get("mark_window_pct", 1.5))
        universe = self._engine_universe()
        ms = int(now.timestamp() * 1000)
        if self.recorder is not None:
            for sym in universe:
                d = self.market.digest(sym, pct)
                if d is not None:
                    self.recorder.write("OptMarks", d, ms)
        lag = self.market.feed_lag()
        inputs = EngineInputs(now=now, feed_mode=lag["mode"], lag_s=lag["lag_s"], vix=self.vix_last,
                              gamma={s: g for s, g in sorted(self.gamma_by_symbol.items()) if s in universe}, universe=universe)
        if self.recorder is not None:
            self.recorder.write("Tick", {"feed_mode": inputs.feed_mode, "lag_s": inputs.lag_s, "vix": inputs.vix, "gamma": inputs.gamma,
                                         "universe": universe, "final": final}, ms)
        before = len(self.engine.events)
        self.engine.on_minute(inputs, self.market)
        if final:
            self.engine.close_all(now)
        events = self.engine.events[before:]          # incl. the forced closes of the final tick
        self.engine_ticks += 1
        self._persist_engine(now)
        for ev in events:
            if ev.get("type") == "alert":
                try:
                    await self.notifier.send("alert", ev["text"], now)
                except Exception as e:  # noqa: BLE001
                    self._record_error("alert", e)
        await self._execution_events(events, inputs)

    def _persist_engine(self, now: datetime) -> None:
        assert self.engine is not None
        td = self.engine.cfg.trade_date.isoformat()
        for pos in self.engine.positions.values():
            if pos.status == "open" or getattr(pos, "_persisted_status", None) != pos.status:
                self.store.upsert_engine_trade(pos.row(), self.run_id, now)
                pos._persisted_status = pos.status  # type: ignore[attr-defined]
        new = self.engine.decisions[self._decisions_persisted:]
        if new:
            self.store.insert_engine_decisions(new, td, self.run_id)
            self._decisions_persisted = len(self.engine.decisions)

    async def engine_loop(self) -> None:
        """Tick 2 s into every minute from the heartbeat until the report time."""
        assert self.sched is not None
        while not self.stopping:
            nxt = floor_minute(self.now()) + timedelta(minutes=1, seconds=ENGINE_TICK_OFFSET_S)
            await self.clock.sleep_until(nxt)
            if self.stopping or self.now() >= self.sched.report:
                return
            try:
                await self.engine_tick(nxt)
            except Exception as e:  # noqa: BLE001
                self._record_error("engine", e)

    def engine_summary(self) -> dict[str, Any] | None:
        return self.engine.state() if self.engine is not None else None

    # ------------------------------------------------------------- execution (M4)
    def _option_quote(self, symbol: str) -> tuple[float, float] | None:
        st = self.market.options.get(symbol)
        if st is None or not st.two_sided:
            return None
        return float(st.bid), float(st.ask)  # type: ignore[arg-type]

    async def _notify(self, kind: str, text: str) -> None:
        await self.notifier.send(kind, text, self.now())

    def _set_halt_setting(self, flag: bool) -> None:
        self.store.set_kv("halt", "true" if flag else "false")
        if self.mirror.enabled:
            self.mirror.queue("saa_set_setting", {"p_key": "halt", "p_value": "true" if flag else "false"}, self.now())

    async def load_execution(self) -> None:
        """Build the paper executor on the sandbox account (M4). Anything missing → execution OFF with a stated reason."""
        assert self.sched is not None
        s = self.settings
        if not s.execution_enabled:
            self.execution_off_reason = "SAA_EXECUTION=false"
            log.warning("paper execution OFF: %s", self.execution_off_reason)
            return
        if s.broker_env != "sandbox":
            self.execution_off_reason = f"broker env {s.broker_env!r} is not the sandbox (M5)"
            log.error("paper execution OFF: %s", self.execution_off_reason)
            return
        broker = None
        try:
            if self._broker_factory is not None:
                broker = await self._broker_factory(self._brokerage)
            elif self._brokerage is not None and getattr(self._brokerage, "broker", None) is not None:
                from .execution.tastytrade_broker import TastytradeBroker
                broker = await TastytradeBroker.open(self._brokerage.broker, env=s.broker_env)
        except Exception as e:  # noqa: BLE001
            self._record_error("execution_load", e)
        if broker is None:
            err = getattr(getattr(self._brokerage, "broker_info", None), "error", None) if self._brokerage is not None else None
            self.execution_off_reason = f"sandbox account unavailable ({err or 'no sandbox session'})"
            log.error("paper execution OFF: %s", self.execution_off_reason)
            return
        self.bot = self._bot or TelegramBot(s, self._http, self.clock)
        messenger = self.bot if self.bot.configured else None
        if messenger is None:
            log.warning("Telegram bot not configured — proposals cannot be approved; every entry will be logged as failed")
        account = self.engine.cfg.account if self.engine is not None else 1000.0
        approvals = ApprovalGate(messenger, self.clock, self.store, timeout_s=self.execution_policy.approval_timeout_s, run_id=self.run_id,
                                 trade_date=self.sched.trade_date)
        self.executor = Executor(broker=broker, store=self.store, clock=self.clock, approvals=approvals, killswitch=self.killswitch,
                                 policy=self.execution_policy, account=account, run_id=self.run_id, trade_date=self.sched.trade_date,
                                 quote_fn=self._option_quote, notify=self._notify, on_error=self._record_error, set_halt_setting=self._set_halt_setting)
        # the M1 /halt path sets saa.settings.halt; honour it at start (and the file flag from a previous run)
        halted_setting = False
        if self.mirror.enabled:
            try:
                halted_setting = (await self.mirror.get_setting("halt") or "").strip().lower() == "true"
            except MirrorError as e:
                log.warning("halt setting unavailable (%s)", e)
        if halted_setting and not self.killswitch.engaged:
            self.killswitch.engage("saa.settings.halt was true at session start (/halt via the edge function)", "settings", self.now())
        if self.killswitch.engaged:
            self._halt_handled = True       # nothing to flatten from a previous run here; reconciliation reports any sandbox position
            log.warning("kill switch is ENGAGED at start: %s", self.killswitch.info())
        log.info("paper execution ready: %s %s, %s mode, approval timeout %.0fs, telegram %s", getattr(broker, "name", "?"),
                 getattr(broker, "account_masked", "?"), self.execution_policy.mode, self.execution_policy.approval_timeout_s,
                 "configured" if messenger else "NOT configured")

    async def _execution_events(self, events: list[dict[str, Any]], inputs: EngineInputs) -> None:
        if self.executor is None or not events:
            return
        try:
            await self.executor.on_engine_events(events, feed_mode=inputs.feed_mode, lag_s=inputs.lag_s)
        except Exception as e:  # noqa: BLE001
            self._record_error("execution", e)

    async def reconcile_round(self) -> None:
        if self.executor is None:
            return
        await self.executor.reconcile()
        if self.mirror.enabled and not self.killswitch.engaged:
            try:
                if (await self.mirror.get_setting("halt") or "").strip().lower() == "true":
                    await self.executor.halt("saa.settings.halt is true (/halt via the edge function)", "settings")
                    self._halt_handled = True
            except MirrorError as e:
                log.debug("halt setting check failed: %s", e)

    async def killswitch_round(self) -> None:
        """Every 2 s: a HALT file that appeared from outside (`./run.sh halt`) flattens exactly once; a cleared file re-arms."""
        if self.executor is None:
            return
        if self.killswitch.engaged:
            if not self._halt_handled:
                self._halt_handled = True
                info = self.killswitch.info() or {}
                await self.executor.halt(str(info.get("reason") or "HALT file present"), str(info.get("by") or "file"))
        else:
            self._halt_handled = False

    async def _telegram_loop(self) -> None:
        assert self.bot is not None

        async def load_offset() -> int:
            if self.mirror.enabled:
                v = await self.mirror.get_setting("telegram_update_offset")
                return int(v or 0)
            return int(self.store.get_kv("telegram_update_offset", "0") or 0)

        async def save_offset(offset: int) -> None:
            self.store.set_kv("telegram_update_offset", str(offset))
            if self.mirror.enabled:
                try:
                    await self.mirror.rpc("saa_set_setting", {"p_key": "telegram_update_offset", "p_value": str(offset)})
                except MirrorError:
                    self.mirror.queue("saa_set_setting", {"p_key": "telegram_update_offset", "p_value": str(offset)}, self.now())

        await self.bot.poll(self.stop, on_callback=self._on_tg_callback, on_command=self._on_tg_command, load_offset=load_offset, save_offset=save_offset)

    async def _on_tg_callback(self, data: str, by: str, cb_id: str, message_id: int | None) -> str | None:
        if self.executor is None:
            return "The daemon is not executing — nothing to approve."
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "appr":
            p = await self.executor.approvals.resolve(parts[1], parts[2], by)
            if p is None:
                return "Too late — this proposal already closed."
            return "Approved ✅ placing the paper order" if p.approved else "Skipped ⏭"
        return None

    async def _on_tg_command(self, cmd: str, args: str, by: str) -> str | None:
        ex = self.executor
        if cmd == "/halt":
            if ex is None:
                self.killswitch.engage(f"/halt {args}".strip(), by, self.now())
                self._set_halt_setting(True)
                return "HALT flag set (no paper execution in this session)."
            await ex.halt(f"/halt {args}".strip(), by)
            self._halt_handled = True
            return None    # the executor's HALT report goes out as a system message
        if cmd == "/resume":
            if ex is None:
                self.killswitch.clear(by)
                self._set_halt_setting(False)
                return "HALT flag cleared."
            ok = await ex.resume(by)
            self._halt_handled = False
            return None if ok else "Not halted."
        if cmd == "/status":
            return self.status_text()
        if cmd == "/positions":
            if ex is None:
                return "Paper execution is off this session."
            opened = [t for t in ex.trades.values() if t.remaining > 0 and t.status in ("open", "closing")]
            if not opened:
                return "No open paper positions."
            return "\n".join(f"{t.symbol} {t.option_symbol} ×{t.remaining} @ {t.entry_price} ({t.status})" for t in opened)
        if cmd in ("/help", "/start"):
            return "Commands: /status · /positions · /halt — flatten and stop entries · /resume — re-arm · /id"
        if cmd == "/id":
            return f"chat id: {self.settings.telegram_chat_id}"
        return None

    def status_text(self) -> str:
        lag = self.feed_lag()
        eng = self.engine_summary() or {}
        ex = self.executor.summary() if self.executor is not None else None
        bits = [f"SAA {self.run_id or 'idle'} · feed {lag.get('mode')} {lag.get('lag_s') if lag.get('lag_s') is not None else '—'}s",
                f"engine: {(eng.get('counts') or {}).get('fired', 0)} fired · {(eng.get('positions') or {}).get('open', 0)} shadow open · gate R {eng.get('today_gate_r', 0):+.2f}" if eng else "engine: not loaded",
                (f"paper: {ex['open']} open · {ex['closed']} closed · {ex['realized_r']:+.2f}R · proposals {ex['counts']['proposed']} "
                 f"(✅{ex['counts']['approved']} ⏭{ex['counts']['skipped']} ⏱{ex['counts']['timeout']})" + (" · HALTED" if ex["halted"] else ""))
                if ex else f"paper: OFF ({self.execution_off_reason or 'not loaded'})"]
        return "\n".join(bits)

    def execution_summary(self) -> dict[str, Any] | None:
        if self.executor is not None:
            return self.executor.summary()
        return {"off": True, "reason": self.execution_off_reason} if self.execution_off_reason else None

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
        term = await fetch_vix_term(self._http, self.now(), fallback=getattr(self, "_http_fallback", None))
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
            "econ": self.econ_lines, "mirror": self.mirror.enabled, "lag": self.feed_lag(), "engine": self.engine_summary(),
            "execution": self.execution_summary(), "telegram_bot": self.bot.status() if self.bot is not None else None,
        }

    async def send_heartbeat(self) -> None:
        await self.notifier.send("system", heartbeat_text(self._heartbeat_ctx()), self.now())
        self.mirror.queue("saa_log_run", {"p_job": "daemon:heartbeat", "p_ok": True, "p_detail": {"run_id": self.run_id, "universe": self.index_symbols + self.single_names}}, self.now())

    def _errors_payload(self) -> list[dict[str, Any]]:
        return [{"task": k, "count": v, "first": self.first_error.get(k)} for k, v in sorted(self.errors.items())]

    def feed_lag(self) -> dict[str, Any]:
        return self.market.feed_lag()

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
            "feed": {"events": self.feed_events, "reconnects": self.feed_reconnects, "by_kind": getattr(self.feed, "by_kind", {}), **self.feed_lag()},
            "gamma_open": self.gamma_open, "gamma_close": self.gamma_last, "vix_open": self.vix_first, "vix_close": self.vix_last,
            "halts": self.store.halts_since(sched.open - timedelta(hours=6)) if sched else [],
            "mirror": self.mirror.status() if self._mirror is not None else {"enabled": False}, "telegram": list(self.notifier.sent) if self._notifier else [],
            "broker": vars(self._brokerage.broker_info) if self._brokerage is not None else {}, "data": vars(self._brokerage.data_info) if self._brokerage is not None else {},
            "engine": self.engine_summary(), "engine_ticks": self.engine_ticks,
            "execution": self.execution_summary(), "telegram_bot": self.bot.status() if self.bot is not None else None,
            "recording": str(self.recorder.path) if self.recorder is not None else None,
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
        self.market.session_open = sched.open
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
        if self.settings.record_events:
            self.recorder = Recorder(self.settings.state_dir / "recordings" / f"{self.run_id}.jsonl", underlying_symbols=set())

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

        # 3. universe + chains + feed + engine
        await self.load_universe()
        self.market.underlyings |= set(self.index_symbols + self.single_names)
        if self.recorder is not None:
            self.recorder.symbols |= set(self.index_symbols + self.single_names)
        delta = await self.discover_chains(self.index_symbols + self.single_names)
        self.feed_plan = FeedPlan(set(self.index_symbols + self.single_names), set(self.index_symbols + self.single_names), set(), sched.open)
        self.feed_plan.merge(delta)
        try:
            await self.load_engine()
        except Exception as e:  # noqa: BLE001
            self._record_error("engine_load", e)
        try:
            await self.load_execution()
        except Exception as e:  # noqa: BLE001
            self._record_error("execution_load", e)
            self.execution_off_reason = self.execution_off_reason or f"{type(e).__name__}: {str(e)[:120]}"
        if self._feed_factory is not None:
            self.feed = self._feed_factory(self._brokerage)
        elif self._brokerage.data is not None:
            from .feed import DXLinkFeed
            self.feed = DXLinkFeed(self._brokerage.data, recorder=None, now_ms=self.now_ms)   # the daemon records in on_event

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
            asyncio.create_task(self.supervise("engine", self.engine_loop), name="engine"),
        ]
        if self.executor is not None:
            tasks += [
                asyncio.create_task(self.every("reconcile", self.execution_policy.reconcile_seconds, self.reconcile_round), name="reconcile"),
                asyncio.create_task(self.every("killswitch", 2.0, self.killswitch_round), name="killswitch"),
            ]
            if self.bot is not None and self.bot.configured:
                tasks.append(asyncio.create_task(self.supervise("telegram", self._telegram_loop), name="telegram"))
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
            await self.engine_tick(self.now(), final=True)     # final tick: close anything still open at the bid, record it
        except Exception as e:  # noqa: BLE001
            self._record_error("engine", e)
        try:
            await self._engine_end_of_day()
        except Exception as e:  # noqa: BLE001
            self._record_error("engine", e)
        if self.executor is not None:
            try:
                await self.executor.end_of_day()                 # expire pending proposals, close anything still open, reconcile
                await self.executor.reconcile()
            except Exception as e:  # noqa: BLE001
                self._record_error("execution", e)
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
        if self.executor is not None:
            try:
                await self.executor.shutdown()
            except Exception as e:  # noqa: BLE001
                self._record_error("execution", e)
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
        eng = stats.get("engine") or {}
        self.mirror.queue("saa_log_run", {"p_job": "daemon:session", "p_ok": ok, "p_detail": {
            "run_id": self.run_id, "unhandled": self.unhandled, "errors": self.errors,
            "bars": {k: {"complete": v.get("complete"), "expected": v.get("expected"), "missing": v.get("missing")} for k, v in stats["bars"].items()},
            "snapshots": stats["snapshots"], "snapshots_expected": stats["snapshots_expected"], "n_options": stats["n_options"],
            "feed": stats["feed"], "mirror": stats["mirror"],
            "engine": {"rules_version": (eng.get("rules") or {}).get("version"), "feed_mode": eng.get("feed_mode"), "ticks": eng.get("ticks"),
                       "observe_only_ticks": eng.get("observe_only_ticks"), "counts": eng.get("counts"), "positions": eng.get("positions"),
                       "today_gate_r": eng.get("today_gate_r"), "today_fast_lane_r": eng.get("today_fast_lane_r"), "rails": eng.get("rails"),
                       "stand_down_reasons": eng.get("stand_down_reasons")} if eng else None,
            "execution": stats.get("execution")}}, self.now())
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
        if self.recorder is not None:
            self.recorder.close()
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        log.info("run %s finished: unhandled=%d errors=%s", self.run_id, self.unhandled, self.errors or "none")
        return RunResult(self.run_id, today, ok, stats, self.unhandled, dict(self.errors), list(self.notifier.sent))

    async def _engine_end_of_day(self) -> None:
        """Persist the cooling-off trigger for the next session (SQLite + saa.settings) and log the engine's day."""
        if self.engine is None or self.sched is None:
            return
        td = self.sched.trade_date.isoformat()
        if self.engine.rails.cooling_off_triggered:
            self.store.set_kv("engine_cooling_off_after", td)
            if self.mirror.enabled:
                self.mirror.queue("saa_set_setting", {"p_key": "engine_cooling_off_after", "p_value": td}, self.now())
        st = self.engine.state()
        log.info("engine day %s: %s fired, %s fast-lane, %s closes, gate R %+.2f, fast-lane R %+.2f, stand-downs %s", td, st["counts"]["fired"],
                 st["counts"]["fast_lane_opens"], st["counts"]["closes"], st["today_gate_r"], st["today_fast_lane_r"], st["stand_down_reasons"])
