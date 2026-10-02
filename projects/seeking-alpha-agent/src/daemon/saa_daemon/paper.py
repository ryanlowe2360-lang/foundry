"""Paper-execution self-tests Ryan runs from the Mac against the real sandbox (M4 acceptance evidence), plus the
`halt` / `resume` / `paper-status` commands. Everything here is injectable (broker, quote source, clock, bot) so the
flows are exercised offline in tests; the CLI wires the real tastytrade / DXLink / Telegram objects in.

    ./run.sh paper-roundtrip [--symbol SPY] [--n 3] [--allow-delayed]   buy 1 contract at mid (ladder) → sell to close → reconcile
    ./run.sh halt-test       [--symbol SPY] [--allow-delayed]           buy 1 contract, then the kill-switch flatten; time to flat
    ./run.sh approval-test   [--timeout 180]                            a real Telegram proposal; Approve / Skip / timeout = Skip
    ./run.sh halt | resume                                              engage / clear the kill switch (file flag + saa.settings.halt)
    ./run.sh paper-status                                               today's paper book from SQLite

Order path gate (D19): the entry quote comes from the production DXLink feed; if the feed is not measurably real-time
the tests refuse unless `--allow-delayed` is given — then they say so loudly and record `feed_mode` with the evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from .clock import Clock, et
from .config import Settings
from .execution import ApprovalGate, ExecutionPolicy, Executor, KillSwitch, LadderPolicy, OrderManager, PaperTrade, Ticket
from .execution.broker import Broker, BrokerError
from .execution.symbols import streamer_to_occ
from .mirror import MirrorError, SupabaseMirror
from .store import Store

log = logging.getLogger("saa.paper")

QuoteProbe = Callable[[str], Awaitable[tuple[float, float] | None]]      # option streamer symbol → (bid, ask)


@dataclass
class FeedCheck:
    mode: str              # realtime | DELAYED | unknown
    lag_s: float | None
    note: str


async def measure_feed_lag(data_session: Any, symbols: list[str], *, window_s: float = 6.0, session_open: datetime | None = None,
                           now_fn: Callable[[], datetime] | None = None) -> FeedCheck:
    """Median exchange→receipt delay of a few underlying trades (same rule as the daemon's feed-lag monitor)."""
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Trade

    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    samples: list[float] = []
    open_ms = int(session_open.timestamp() * 1000) if session_open else 0
    try:
        async with DXLinkStreamer(data_session) as st:
            await st.subscribe(Trade, symbols)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + window_s
            while loop.time() < deadline and len(samples) < 8:
                try:
                    t = await asyncio.wait_for(st.get_event(Trade), timeout=max(0.05, deadline - loop.time()))
                except TimeoutError:
                    break
                tms = int(getattr(t, "time", 0) or 0)
                if tms > 0 and tms >= open_ms:
                    samples.append(now_fn().timestamp() - tms / 1000.0)
    except Exception as e:  # noqa: BLE001
        return FeedCheck("unknown", None, f"lag probe failed: {type(e).__name__}: {str(e)[:120]}")
    if not samples:
        return FeedCheck("unknown", None, "no same-session trades seen (outside regular hours?) — lag not measurable")
    med = round(statistics.median(samples), 1)
    return FeedCheck("realtime" if med < 30 else "DELAYED", med, f"{len(samples)} trades, median {med}s")


async def pick_option(brokerage: Any, symbol: str, today: date, *, settings: Settings) -> tuple[str, float]:
    """Nearest-ATM call on the first live expiration of `symbol` → (streamer symbol, spot)."""
    from .chains import from_sdk_nested, plan_chain

    spots = await brokerage.spot_prices([symbol])
    spot = spots.get(symbol)
    if not spot:
        raise RuntimeError(f"no spot price for {symbol}")
    nested = await brokerage.nested_chain(symbol)
    if nested is None:
        raise RuntimeError(f"no option chain for {symbol}")
    plan = plan_chain(symbol, from_sdk_nested(nested), spot, today=today, now_et=et(datetime.now(timezone.utc)).time(), n_exp=2,
                      window_pct=settings.strike_window_pct_index, max_per_side=settings.max_strikes_per_side)
    live = [e for e in plan.expirations if e.expiration >= today and e.strikes]
    if not live:
        raise RuntimeError(f"no live expiration for {symbol}")
    e = live[0] if live[0].expiration > today or len(live) == 1 else live[0]
    sp = min(e.strikes, key=lambda s: (abs(s.strike - spot), s.strike))
    return sp.call, float(spot)


async def probe_option_quote(data_session: Any, option_symbol: str, *, window_s: float = 6.0) -> tuple[float, float] | None:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote

    async with DXLinkStreamer(data_session) as st:
        await st.subscribe(Quote, [option_symbol])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + window_s
        best: tuple[float, float] | None = None
        while loop.time() < deadline:
            try:
                q = await asyncio.wait_for(st.get_event(Quote), timeout=max(0.05, deadline - loop.time()))
            except TimeoutError:
                break
            if q.event_symbol == option_symbol and q.bid_price and q.ask_price and float(q.ask_price) >= float(q.bid_price) > 0:
                best = (float(q.bid_price), float(q.ask_price))
                break
        return best


# ------------------------------------------------------------------------------------------------------- flows
@dataclass
class RoundTrip:
    symbol: str
    option_symbol: str
    occ: str
    entry: dict[str, Any]
    exit: dict[str, Any]
    reconciled: bool
    seconds: float
    realized_pnl: float | None
    realized_r: float | None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.entry.get("status") == "filled" and self.exit.get("status") == "filled" and self.reconciled

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "option_symbol": self.option_symbol, "occ": self.occ, "entry": self.entry, "exit": self.exit,
                "reconciled": self.reconciled, "seconds": self.seconds, "realized_pnl": self.realized_pnl, "realized_r": self.realized_r, "ok": self.ok, "note": self.note}


async def roundtrip(broker: Broker, store: Store, clock: Clock, option_symbol: str, quote: tuple[float, float], *, run_id: str, trade_date: date,
                    ladder: LadderPolicy | None = None, quote_fn: Callable[[], tuple[float, float] | None] | None = None,
                    mirror: SupabaseMirror | None = None, feed: FeedCheck | None = None) -> RoundTrip:
    """One sandbox round trip through the real ladder: buy 1 at mid → sell 1 to close → positions/orders reconciled."""
    om = OrderManager(broker, store, clock, ladder or LadderPolicy(), run_id=run_id, trade_date=trade_date)
    occ = streamer_to_occ(option_symbol)
    t0 = clock.now()
    key = f"roundtrip|{option_symbol}|{et(t0):%H%M%S}"
    entry = await om.work(Ticket.new(key, option_symbol, "buy", 1, quote[0], quote[1], clock.now(), kind="entry"), quote_fn=quote_fn)
    exit_ = Ticket.new(key, option_symbol, "sell", 1, quote[0], quote[1], clock.now(), kind="exit", seq=1)
    if entry.filled_quantity > 0:
        exit_ = await om.work(exit_, quote_fn=quote_fn)
        if exit_.filled_quantity < entry.filled_quantity:
            left = entry.filled_quantity - exit_.filled_quantity
            fl = Ticket.new(key, option_symbol, "sell", left, quote[0], quote[1], clock.now(), kind="flatten", seq=2)
            fl = await om.flatten(fl, quote_fn=quote_fn)
            if fl.filled_quantity:
                exit_.fills += fl.fills
                exit_.filled_quantity += fl.filled_quantity
                exit_.avg_fill_price = round(sum(f["quantity"] * f["price"] for f in exit_.fills) / exit_.filled_quantity, 4)
                exit_.status = "filled" if exit_.filled_quantity >= entry.filled_quantity else exit_.status
    else:
        exit_.status, exit_.reason = "skipped", "entry did not fill"
    # reconcile: nothing of ours left at the broker
    note = ""
    try:
        pos = await broker.positions()
        live = await broker.live_orders()
        ours_pos = [p for p in pos if p.symbol == occ]
        ours_live = [o for o in live if o.symbol == occ]
        reconciled = not ours_pos and not ours_live
        if not reconciled:
            note = f"left at the broker: positions {[(p.symbol.strip(), p.quantity) for p in ours_pos]}, live orders {[o.order_id for o in ours_live]}"
    except BrokerError as e:
        reconciled, note = False, f"reconcile failed: {e}"
    seconds = round(clock.now().timestamp() - t0.timestamp(), 1)
    pnl = r = None
    if entry.filled_quantity and exit_.filled_quantity and entry.avg_fill_price and exit_.avg_fill_price:
        cost = entry.filled_quantity * entry.avg_fill_price * 100.0
        pnl = round(exit_.filled_quantity * exit_.avg_fill_price * 100.0 - cost - entry.fees - exit_.fees, 2)
        r = round(pnl / cost, 4) if cost else None
    rt = RoundTrip(option_symbol[1:].rstrip("0123456789CP.") or option_symbol, option_symbol, occ, entry.row(), exit_.row(), reconciled, seconds, pnl, r, note)
    # a paper_trades row so the round trip shows on the dashboard as sandbox evidence
    now = clock.now()
    trade = PaperTrade(engine_key=key, trade_date=trade_date, symbol=rt.symbol, option_symbol=option_symbol, occ=occ, direction="long", option_type="call",
                       window="roundtrip", lane="roundtrip", contracts=1, account=0.0, shadow_entry_bid=quote[0], shadow_entry_ask=quote[1], created_at=t0,
                       sizing_mode="test", status="closed" if rt.ok else ("open" if entry.filled_quantity and not exit_.filled_quantity else entry.status),
                       decision="cli", entry=entry.row(), exits=[exit_.row()], entry_qty=entry.filled_quantity, entry_price=entry.avg_fill_price,
                       entry_at=entry.done_at, exit_qty=exit_.filled_quantity, exit_price=exit_.avg_fill_price, exit_at=exit_.done_at,
                       exit_reason="roundtrip", realized_pnl=pnl, realized_r=r, fees=round(entry.fees + exit_.fees, 4),
                       notes=[f"paper-roundtrip self-test · feed {feed.mode if feed else 'n/a'} · reconciled={reconciled}"] + ([note] if note else []))
    if entry.avg_fill_price is not None:
        trade.slippage_entry = round(entry.avg_fill_price - quote[1], 4)
    if exit_.avg_fill_price is not None:
        trade.slippage_exit = round(exit_.avg_fill_price - quote[0], 4)
    store.upsert_paper_trade(trade.row(), trade_date.isoformat(), run_id, now)
    if mirror is not None and mirror.enabled:
        try:
            await mirror.flush_paper_orders()
            await mirror.flush_paper_trades()
        except MirrorError as e:
            log.warning("mirror flush failed: %s", e)
    return rt


async def halt_test(broker: Broker, store: Store, clock: Clock, option_symbol: str, quote: tuple[float, float], *, run_id: str, trade_date: date,
                    state_dir: Any, ladder: LadderPolicy | None = None, quote_fn: Callable[[str], tuple[float, float] | None] | None = None,
                    notify: Callable[[str, str], Awaitable[Any]] | None = None) -> dict[str, Any]:
    """Open 1 contract, then the real kill-switch path (`Executor.halt`): positions flat, time to flat measured."""
    policy = ExecutionPolicy(ladder=ladder or LadderPolicy())
    kill = KillSwitch(state_dir)
    was_engaged = kill.engaged
    gate = ApprovalGate(None, clock, store, timeout_s=1, run_id=run_id, trade_date=trade_date)
    ex = Executor(broker=broker, store=store, clock=clock, approvals=gate, killswitch=kill, policy=policy, account=1000.0, run_id=run_id,
                  trade_date=trade_date, quote_fn=(quote_fn or (lambda _s: quote)), notify=notify)
    occ = streamer_to_occ(option_symbol)
    key = f"halttest|{option_symbol}|{et(clock.now()):%H%M%S}"
    entry = await ex.orders.work(Ticket.new(key, option_symbol, "buy", 1, quote[0], quote[1], clock.now(), kind="entry"),
                                 quote_fn=(lambda: quote_fn(option_symbol)) if quote_fn else None)
    out: dict[str, Any] = {"option_symbol": option_symbol, "occ": occ, "entry": entry.row()}
    if entry.filled_quantity <= 0:
        out.update(ok=False, note=f"entry did not fill ({entry.status}: {entry.reason}); nothing to flatten")
        return out
    t = PaperTrade(engine_key=key, trade_date=trade_date, symbol=option_symbol, option_symbol=option_symbol, occ=occ, direction="long", option_type="call",
                   window="halttest", lane="halttest", contracts=1, account=1000.0, shadow_entry_bid=quote[0], shadow_entry_ask=quote[1], created_at=clock.now(),
                   status="open", entry=entry.row(), entry_qty=entry.filled_quantity, entry_price=entry.avg_fill_price, entry_at=entry.done_at)
    ex.trades[key] = t
    store.upsert_paper_trade(t.row(), trade_date.isoformat(), run_id, clock.now())
    before = await broker.positions()
    out["position_before"] = [(p.symbol.strip(), p.quantity) for p in before if p.symbol == occ]
    rec = await ex.halt("halt-test (CLI)", "cli")
    after = await broker.positions()
    out["position_after"] = [(p.symbol.strip(), p.quantity) for p in after if p.symbol == occ]
    out.update(halt=rec, trade=t.row(), ok=bool(rec.get("ok")) and not out["position_after"] and rec.get("seconds", 99) <= policy.halt_budget_s,
               seconds=rec.get("seconds"), within_10s=rec.get("seconds", 99) <= policy.halt_budget_s)
    if not was_engaged:
        kill.clear("halt-test")       # the test must not leave the daemon halted
        out["kill_switch_cleared"] = True
    return out


async def approval_test(bot: Any, store: Store, clock: Clock, *, timeout_s: float, run_id: str, trade_date: date,
                        load_offset: Callable[[], Awaitable[int]] | None = None, save_offset: Callable[[int], Awaitable[None]] | None = None,
                        keepalive: Callable[[], Awaitable[None]] | None = None) -> dict[str, Any]:
    """A real proposal with buttons; wait for Approve / Skip / the timeout (= Skip) while polling the bot ourselves."""
    gate = ApprovalGate(bot, clock, store, timeout_s=timeout_s, run_id=run_id, trade_date=trade_date)
    p = await gate.propose("approval-test", "TEST", f"Proposed ▸ TEST approval flow (no order) · {et(clock.now()):%H:%M:%S} ET\n"
                                                      f"Tap Approve or Skip, or wait {int(timeout_s)} s to see the timeout logged as Skip.")
    if p.status != "pending":
        return {"ok": False, "status": p.status, "note": p.note}
    stop = asyncio.Event()

    async def on_callback(data: str, by: str, cb_id: str, message_id: int | None) -> str | None:
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "appr":
            res = await gate.resolve(parts[1], parts[2], by)
            if res is not None:
                stop.set()
                return "Approved ✅ (test)" if res.approved else "Skipped ⏭ (test)"
            return "Too late."
        return None

    async def on_command(cmd: str, args: str, by: str) -> str | None:
        return f"approval-test running — tap the buttons above ({cmd} ignored)"

    poll = asyncio.create_task(bot.poll(stop, on_callback=on_callback, on_command=on_command, load_offset=load_offset, save_offset=save_offset))

    async def keep() -> None:
        while not stop.is_set():
            if keepalive is not None:
                try:
                    await keepalive()
                except Exception as e:  # noqa: BLE001
                    log.debug("keepalive failed: %s", e)
            await clock.sleep(60.0)

    ka = asyncio.create_task(keep())
    try:
        status = await gate.wait(p)
    finally:
        stop.set()
        for task in (poll, ka):
            task.cancel()
        await asyncio.gather(poll, ka, return_exceptions=True)
    return {"ok": True, "status": status, "proposal_id": p.proposal_id, "latency_s": p.latency_s, "decided_by": p.decided_by, "note": p.note,
            "logged_as": "skip (timeout)" if status == "timeout" else status}


def paper_status_text(store: Store, today: date) -> str:
    trades = store.paper_trades(today.isoformat())
    orders = store.paper_orders(today.isoformat())
    appr = store.approvals(today.isoformat())
    recs = store.reconciliations(today.isoformat())
    lines = [f"Paper book {today}: {len(trades)} trade(s), {len(orders)} order ticket(s), {len(appr)} proposal(s), {len(recs)} reconciliation(s)"]
    for t in trades:
        p = t["payload"]
        lines.append(f"  {p['engine_key']}: {p['status']} · qty {p.get('entry_qty', 0)}/{p.get('exit_qty', 0)} @ {p.get('entry_price')}→{p.get('exit_price')}"
                     f" · R {p.get('realized_r')} · {p.get('block_reason') or p.get('exit_reason') or ''}")
    for a in appr:
        p = a["payload"]
        lines.append(f"  proposal {p['proposal_id']} {p['symbol']}: {p['status']} ({p.get('latency_s')}s) {p.get('note') or ''}")
    if recs:
        last = recs[-1]["payload"]
        lines.append(f"  last reconcile {last.get('ts')}: ok={last.get('ok')} mismatches={len(last.get('mismatches') or [])}")
    halt = KillSwitch(store.path.parent).info()
    lines.append(f"  kill switch: {'ENGAGED ' + json.dumps(halt) if halt else 'armed'}")
    return "\n".join(lines)


__all__ = ["FeedCheck", "RoundTrip", "measure_feed_lag", "pick_option", "probe_option_quote", "roundtrip", "halt_test", "approval_test", "paper_status_text"]
