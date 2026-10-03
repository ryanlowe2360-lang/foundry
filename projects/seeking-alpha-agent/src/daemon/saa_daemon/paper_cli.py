"""CLI wiring for the paper self-tests: real tastytrade sessions, real DXLink probes, real Telegram, real Supabase.
The flows themselves live in `paper.py` (and are tested offline); this module only assembles the pieces and prints
the evidence. Nothing here prints a secret."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .clock import Clock, Schedule, et, is_trading_day
from .config import Settings
from .execution import KillSwitch, LadderPolicy
from .execution.telegram_bot import TelegramBot
from .mirror import MirrorError, SupabaseMirror
from .paper import EntryPick, approval_test, choose_entry, halt_test, measure_feed_lag, paper_status_text, probe_option_quote, roundtrip, sandbox_lookup
from .store import Store

log = logging.getLogger("saa.paper")


class _Ctx:
    def __init__(self, settings: Settings, *, need_broker: bool = True, need_data: bool = True):
        from .http import Httpx2Client
        self.settings = settings
        self.clock = Clock()
        self.store = Store(settings.state_dir / "saa.sqlite")
        self.http = Httpx2Client()
        self.mirror = SupabaseMirror(settings, self.store, self.http)
        self.brokerage: Any = None
        self.broker: Any = None
        self.need_broker, self.need_data = need_broker, need_data
        self.today = et(self.clock.now()).date()
        self.sched = Schedule.for_date(self.today)

    async def __aenter__(self) -> "_Ctx":
        if self.need_broker or self.need_data:
            from .broker import Brokerage
            self.brokerage = Brokerage(self.settings)
            await self.brokerage.open()
            if self.need_data and self.brokerage.data is None:
                raise RuntimeError(f"production data session failed: {self.brokerage.data_info.error}")
            if self.need_broker:
                if self.brokerage.broker is None:
                    raise RuntimeError(f"sandbox session failed: {self.brokerage.broker_info.error}")
                from .execution.tastytrade_broker import TastytradeBroker
                self.broker = await TastytradeBroker.open(self.brokerage.broker, env=self.settings.broker_env)
                bi = self.brokerage.broker_info
                print(f"sandbox account {self.broker.account_masked} ({bi.account_type or '?'}, options level {bi.options_level or '?'})")
                try:
                    print(f"balances: {json.dumps(await self.broker.balances())}")
                except Exception as e:  # noqa: BLE001
                    print(f"balances unavailable: {type(e).__name__}: {str(e)[:120]}")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.brokerage is not None:
            await self.brokerage.close()
        await self.http.aclose()

    async def log_run(self, job: str, ok: bool, detail: dict[str, Any]) -> None:
        if not self.mirror.enabled:
            print("(mirror off — evidence stays in SQLite only)")
            return
        try:
            await self.mirror.rpc("saa_log_run", {"p_job": job, "p_ok": ok, "p_detail": detail})
            print(f"logged saa.run_log {job} ok={ok}")
        except MirrorError as e:
            print(f"run_log write failed ({e}); queued for the next session")
            self.mirror.queue("saa_log_run", {"p_job": job, "p_ok": ok, "p_detail": detail}, self.clock.now())

    def hours_reason(self) -> str:
        """Non-empty when the clock is outside a trading day's regular session — the sandbox parks orders then."""
        now = self.clock.now()
        if not is_trading_day(self.today):
            return f"{et(now):%a %Y-%m-%d} is not a trading day: the sandbox parks orders for the next session and fills nothing — rerun 09:30–16:00 ET on a trading day"
        if now < self.sched.open or now >= self.sched.close:
            return (f"{et(now):%H:%M} ET is outside regular hours ({et(self.sched.open):%H:%M}–{et(self.sched.close):%H:%M}): the sandbox parks orders for the "
                    "next session and fills nothing — rerun inside the session")
        return ""

    def hours_notice(self) -> None:
        why = self.hours_reason()
        if why:
            print(f"⚠ {why}; this run proves placement → cancel → reconcile only")

    async def entry_quote(self, symbol: str, allow_delayed: bool) -> tuple[EntryPick, Any]:
        """Feed gate, then the validated instrument choice (both chains → sandbox lookup → DXLink quote → sandbox dry
        run). Raises when the feed is not real-time and --allow-delayed is absent, or when no candidate passes."""
        feed = await measure_feed_lag(self.brokerage.data, [symbol], session_open=self.sched.open)
        print(f"feed lag: {feed.mode} ({feed.note})")
        if feed.mode != "realtime":
            if not allow_delayed:
                raise RuntimeError("the production feed is not measurably real-time (D19) — re-run with --allow-delayed to test the sandbox "
                                   "plumbing on a delayed/after-hours quote (fills are sandbox fills; the quote is not a decision)")
            print("⚠ --allow-delayed: sandbox plumbing test on a non-real-time quote; recorded as such")
        print(f"choosing an option on {symbol} the sandbox trades:")
        try:
            pick = await choose_entry(self.brokerage, symbol, self.today, now_et=et(self.clock.now()).time(),
                                      probe=lambda sym: probe_option_quote(self.brokerage.data, sym),
                                      lookup=sandbox_lookup(self.brokerage.broker), dry_run=self.broker.dry_run)
        except RuntimeError as e:
            print(f"  {e}")
            raise RuntimeError("no option the sandbox trades could be found — see the diagnosis above") from None
        for line in pick.lines:
            print(f"  {line}")
        print(f"{symbol} spot {pick.spot:.2f} → {pick.symbol} bid {pick.quote[0]:.2f} / ask {pick.quote[1]:.2f}")
        return pick, feed


async def run_roundtrip(settings: Settings, *, symbol: str, n: int, allow_delayed: bool) -> int:
    async with _Ctx(settings) as c:
        pick, feed = await c.entry_quote(symbol, allow_delayed)
        option_symbol, quote = pick.symbol, pick.quote
        c.hours_notice()
        run_id = f"paper-roundtrip-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        results = []
        for i in range(max(1, n)):
            print(f"--- round trip {i + 1}/{n} ---")
            rt = await roundtrip(c.broker, c.store, c.clock, option_symbol, quote, run_id=run_id, trade_date=c.today, ladder=LadderPolicy(),
                                 mirror=c.mirror, feed=feed)
            e, x = rt.entry, rt.exit
            print(f"entry {e['status']}: {e['filled_quantity']}/{e['quantity']} @ {e['avg_fill_price']} ladder {e['limit_prices']} orders {e['broker_order_ids']} ({e['reason']})")
            print(f"exit  {x['status']}: {x['filled_quantity']}/{x['quantity']} @ {x['avg_fill_price']} ladder {x['limit_prices']} orders {x['broker_order_ids']} ({x['reason']})")
            print(f"reconciled: {rt.reconciled} · {rt.seconds}s · P&L {rt.realized_pnl} ({rt.realized_r}R) {rt.note}")
            results.append(rt.as_dict())
            if not rt.ok:
                break
        ok = all(r["ok"] for r in results) and len(results) == max(1, n)
        await c.log_run("paper:roundtrip", ok, {"run_id": run_id, "symbol": symbol, "option_symbol": option_symbol, "quote": quote, "pick": pick.as_dict(),
                                                "feed": {"mode": feed.mode, "lag_s": feed.lag_s, "note": feed.note}, "allow_delayed": allow_delayed,
                                                "account": c.broker.account_masked, "round_trips": results, "n_ok": sum(1 for r in results if r["ok"])})
        print("RESULT:", "ALL ROUND TRIPS FILLED AND RECONCILED" if ok else ("NOT OK — " + (c.hours_reason() or "see above")))
        return 0 if ok else 1


async def run_halt_test(settings: Settings, *, symbol: str, allow_delayed: bool) -> int:
    async with _Ctx(settings) as c:
        pick, feed = await c.entry_quote(symbol, allow_delayed)
        option_symbol, quote = pick.symbol, pick.quote
        c.hours_notice()
        run_id = f"paper-halt-test-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        out = await halt_test(c.broker, c.store, c.clock, option_symbol, quote, run_id=run_id, trade_date=c.today, state_dir=settings.state_dir)
        e = out["entry"]
        print(f"entry {e['status']}: {e['filled_quantity']}/{e['quantity']} @ {e['avg_fill_price']} ({e['reason']})")
        if "halt" in out:
            h = out["halt"]
            print(f"HALT: flat={h['ok']} in {h['seconds']}s (budget 10 s: {'✓' if out['within_10s'] else '✗'}) · closed {h['closed']} · position before {out['position_before']} → after {out['position_after']}")
        else:
            print(out.get("note"))
        try:
            await c.mirror.flush_paper_orders()
            await c.mirror.flush_paper_trades()
        except MirrorError as ex:
            print(f"mirror flush failed: {ex}")
        await c.log_run("paper:halt_test", bool(out.get("ok")), {"run_id": run_id, "symbol": symbol, "option_symbol": option_symbol, "quote": quote, "pick": pick.as_dict(),
                                                                 "feed": {"mode": feed.mode, "lag_s": feed.lag_s}, "allow_delayed": allow_delayed,
                                                                 "account": c.broker.account_masked, **{k: v for k, v in out.items() if k != "trade"}})
        print("RESULT:", "FLAT WITHIN 10 S" if out.get("ok") else ("NOT OK — " + (c.hours_reason() or "see above")))
        return 0 if out.get("ok") else 1


async def run_approval_test(settings: Settings, *, timeout_s: float) -> int:
    from .http import Httpx2Client
    clock = Clock()
    store = Store(settings.state_dir / "saa.sqlite")
    http = Httpx2Client()
    mirror = SupabaseMirror(settings, store, http)
    bot = TelegramBot(settings, http, clock)
    if not bot.configured:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not both set — the approval flow needs the bot")
        return 2
    run_id = f"paper-approval-test-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    today = et(clock.now()).date()

    async def load_offset() -> int:
        if mirror.enabled:
            return int(await mirror.get_setting("telegram_update_offset") or 0)
        return int(store.get_kv("telegram_update_offset", "0") or 0)

    async def save_offset(o: int) -> None:
        store.set_kv("telegram_update_offset", str(o))
        if mirror.enabled:
            await mirror.rpc("saa_set_setting", {"p_key": "telegram_update_offset", "p_value": str(o)})

    async def keepalive() -> None:
        # keeps saa.settings.daemon_last_seen fresh so the telegram-send edge function leaves getUpdates to us
        if mirror.enabled:
            await mirror.rpc("saa_daemon_run", {"p_run_id": run_id, "p_patch": {"trade_date": today.isoformat(), "mode": "approval-test", "host": "cli"}})

    try:
        await keepalive()
        print(f"sending a test proposal to Telegram; waiting up to {int(timeout_s)} s for Approve / Skip …")
        out = await approval_test(bot, store, clock, timeout_s=timeout_s, run_id=run_id, trade_date=today, load_offset=load_offset, save_offset=save_offset,
                                  keepalive=keepalive)
        print(json.dumps(out, indent=2, default=str))
        try:
            await mirror.flush_approvals()
            if mirror.enabled:
                await mirror.rpc("saa_daemon_run", {"p_run_id": run_id, "p_patch": {"status": "done", "ended_at": clock.now().isoformat()}})
                await mirror.rpc("saa_log_run", {"p_job": "paper:approval_test", "p_ok": bool(out.get("ok")), "p_detail": out})
                print("logged saa.run_log paper:approval_test")
        except MirrorError as e:
            print(f"mirror write failed: {e}")
        return 0 if out.get("ok") else 1
    finally:
        await http.aclose()


async def run_halt_flag(settings: Settings, *, engage: bool, reason: str) -> int:
    from .http import Httpx2Client
    clock = Clock()
    store = Store(settings.state_dir / "saa.sqlite")
    kill = KillSwitch(settings.state_dir)
    http = Httpx2Client()
    mirror = SupabaseMirror(settings, store, http)
    try:
        if engage:
            fresh = kill.engage(reason or "./run.sh halt", "cli", clock.now())
            print(f"kill switch {'ENGAGED' if fresh else 'already engaged'}: {kill.path} — a running daemon flattens within 2 s and refuses new entries")
        else:
            cleared = kill.clear("cli")
            print(f"kill switch {'cleared' if cleared else 'was not engaged'}")
        store.set_kv("halt", "true" if engage else "false")
        if mirror.enabled:
            try:
                await mirror.rpc("saa_set_setting", {"p_key": "halt", "p_value": "true" if engage else "false"})
                print(f"saa.settings.halt = {'true' if engage else 'false'}")
            except MirrorError as e:
                print(f"saa.settings.halt not updated ({e})")
        return 0
    finally:
        await http.aclose()


def run_paper_status(settings: Settings) -> int:
    store = Store(settings.state_dir / "saa.sqlite")
    print(paper_status_text(store, et(datetime.now(timezone.utc)).date()))
    return 0


def main_paper(cmd: str, settings: Settings, args: Any) -> int:
    if cmd == "paper-roundtrip":
        return asyncio.run(run_roundtrip(settings, symbol=args.symbol.upper(), n=args.n, allow_delayed=args.allow_delayed))
    if cmd == "halt-test":
        return asyncio.run(run_halt_test(settings, symbol=args.symbol.upper(), allow_delayed=args.allow_delayed))
    if cmd == "approval-test":
        return asyncio.run(run_approval_test(settings, timeout_s=float(args.timeout)))
    if cmd == "halt":
        return asyncio.run(run_halt_flag(settings, engage=True, reason=args.reason))
    if cmd == "resume":
        return asyncio.run(run_halt_flag(settings, engage=False, reason=""))
    if cmd == "paper-status":
        return run_paper_status(settings)
    return 2


__all__ = ["main_paper"]
