"""The executor: turns the engine's gate-fired `open` / `bank` / `close` events into paper orders in the sandbox.

Order of defences on an entry, every one logged as a `saa.paper_trades` row:
  1. kill switch engaged → `blocked: halt`;
  2. feed not real-time → `blocked: feed_not_realtime` (the same gate as the engine, D19 — belt and braces);
  3. mode must be `approval` (auto is M6; refused in code until then);
  4. Tier 1 order caps re-checked at the live ask (≤ 2 contracts per $1k, ≤ 50 % of the account) → `refused`;
  5. Telegram proposal → Approve / Skip / 3-minute timeout = Skip / expired if the engine closed first;
  6. the limit-at-mid ladder; fills logged; the engine's `close` (or `bank`) event drives the exit ladder, escalating to the
     flatten ladder if an exit will not fill — an exit always completes.

`/halt` (or the HALT file): pending proposals expire, working orders are cancelled, every open paper position — and any
sandbox position the book does not know — is flattened with the bid → bid − step → market ladder, and the time to flat is
reported (spec: within 10 s). Reconciliation every 30 s compares the broker's positions and live orders with the book.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from ..clock import Clock, et
from ..store import Store
from ..engine.tier1 import TIER1, Tier1
from .approvals import ApprovalGate
from .broker import Broker, BrokerError
from .killswitch import KillSwitch
from .orders import LadderPolicy, OrderManager, Ticket
from .symbols import occ_to_streamer, streamer_to_occ

log = logging.getLogger("saa.executor")

ALLOW_AUTO_MODE = False      # M6 flips this (approval bypass inside Tier 1). Until then "auto" is refused in code.

QuoteFn = Callable[[str], tuple[float, float] | None]           # streamer symbol → (bid, ask) or None
NotifyFn = Callable[[str, str], Awaitable[Any]]                 # (kind, text)
ErrorFn = Callable[[str, BaseException], None]


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: str = "approval"                 # approval | auto (auto refused until M6)
    require_realtime: bool = True          # D19
    approval_timeout_s: float = 180.0      # 3 minutes = Skip
    reconcile_seconds: float = 30.0
    halt_budget_s: float = 10.0            # the spec's "flattens within 10 s"
    halt_max_wait_s: float = 60.0
    ladder: LadderPolicy = LadderPolicy()
    tier1: Tier1 = TIER1


@dataclass
class PaperTrade:
    engine_key: str
    trade_date: date
    symbol: str
    option_symbol: str
    occ: str
    direction: str
    option_type: str
    window: str
    lane: str
    contracts: int
    account: float
    shadow_entry_bid: float
    shadow_entry_ask: float
    created_at: datetime
    sizing_mode: str = ""
    probability: float | None = None
    status: str = "new"            # blocked | refused | proposed | skipped | timeout | expired | failed | working | open | closing | closed | unfilled | rejected | error | cancelled | halted
    block_reason: str = ""
    proposal_id: str | None = None
    decision: str | None = None
    decided_at: datetime | None = None
    decision_latency_s: float | None = None
    entry: dict[str, Any] | None = None
    exits: list[dict[str, Any]] = field(default_factory=list)
    entry_qty: int = 0
    entry_price: float | None = None
    entry_at: datetime | None = None
    exit_qty: int = 0
    exit_price: float | None = None     # average across all exits (banks included)
    exit_at: datetime | None = None
    exit_reason: str | None = None
    shadow_exit_bid: float | None = None
    shadow_r: float | None = None
    realized_pnl: float | None = None
    realized_r: float | None = None
    slippage_entry: float | None = None  # fill − shadow ask (negative = better than the engine assumed)
    slippage_exit: float | None = None   # fill − shadow bid
    fees: float = 0.0
    notes: list[str] = field(default_factory=list)
    # runtime (not persisted)
    _entry_ticket: Ticket | None = field(default=None, repr=False)
    _exit_ticket: Ticket | None = field(default=None, repr=False)
    _close_pending: str | None = field(default=None, repr=False)
    _flattening: bool = field(default=False, repr=False)

    @property
    def remaining(self) -> int:
        return max(0, self.entry_qty - self.exit_qty)

    @property
    def cost(self) -> float:
        return self.entry_qty * (self.entry_price or 0.0) * 100.0

    def note(self, s: str) -> None:
        self.notes.append(s)

    def row(self) -> dict[str, Any]:
        return {"engine_key": self.engine_key, "trade_date": self.trade_date.isoformat(), "symbol": self.symbol, "option_symbol": self.option_symbol,
                "occ_symbol": self.occ, "direction": self.direction, "option_type": self.option_type, "window": self.window, "lane": self.lane,
                "contracts": self.contracts, "account": self.account, "shadow_entry_bid": self.shadow_entry_bid, "shadow_entry_ask": self.shadow_entry_ask,
                "created_at": self.created_at.isoformat(), "sizing_mode": self.sizing_mode, "probability": self.probability, "status": self.status,
                "block_reason": self.block_reason, "proposal_id": self.proposal_id, "decision": self.decision,
                "decided_at": self.decided_at.isoformat() if self.decided_at else None, "decision_latency_s": self.decision_latency_s,
                "entry": self.entry, "exits": list(self.exits), "entry_qty": self.entry_qty, "entry_price": self.entry_price,
                "entry_at": self.entry_at.isoformat() if self.entry_at else None, "exit_qty": self.exit_qty, "exit_price": self.exit_price,
                "exit_at": self.exit_at.isoformat() if self.exit_at else None, "exit_reason": self.exit_reason, "shadow_exit_bid": self.shadow_exit_bid,
                "shadow_r": self.shadow_r, "realized_pnl": self.realized_pnl, "realized_r": self.realized_r, "slippage_entry": self.slippage_entry,
                "slippage_exit": self.slippage_exit, "fees": round(self.fees, 4), "notes": list(self.notes)}


class Executor:
    def __init__(self, *, broker: Broker, store: Store, clock: Clock, approvals: ApprovalGate, killswitch: KillSwitch, policy: ExecutionPolicy,
                 account: float, run_id: str, trade_date: date, quote_fn: QuoteFn, notify: NotifyFn | None = None,
                 on_error: ErrorFn | None = None, set_halt_setting: Callable[[bool], None] | None = None):
        if policy.mode not in ("approval", "auto"):
            raise ValueError(f"unknown execution mode {policy.mode!r}")
        if policy.mode == "auto" and not ALLOW_AUTO_MODE:
            raise ValueError("execution mode 'auto' is M6 (approval bypass inside Tier 1); M4–M5 run in approval mode only")
        self.broker = broker
        self.store = store
        self.clock = clock
        self.approvals = approvals
        self.killswitch = killswitch
        self.policy = policy
        self.account = float(account)
        self.run_id = run_id
        self.trade_date = trade_date
        self.quote_fn = quote_fn
        self.notify = notify
        self.on_error = on_error
        self.set_halt_setting = set_halt_setting
        self.orders = OrderManager(broker, store, clock, policy.ladder, run_id=run_id, trade_date=trade_date)
        self.trades: dict[str, PaperTrade] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.counts: dict[str, int] = {k: 0 for k in ("events", "blocked", "refused", "proposed", "approved", "skipped", "timeout", "expired",
                                                       "failed", "filled", "partial", "unfilled", "rejected", "error", "closed", "banks",
                                                       "flattened", "reconciliations", "mismatches", "halts")}

        self.last_reconcile: dict[str, Any] | None = None
        self.halt_events: list[dict[str, Any]] = []
        self.halting = False
        self._alerted_mismatches: set[str] = set()
        self.feed_mode: str | None = None

    # ------------------------------------------------------------------------------------------------- helpers
    @property
    def halted(self) -> bool:
        return self.killswitch.engaged

    def _persist(self, t: PaperTrade) -> None:
        try:
            self.store.upsert_paper_trade(t.row(), t.trade_date.isoformat(), self.run_id, self.clock.now())
        except Exception as e:  # noqa: BLE001
            log.error("paper_trades persist failed for %s: %s", t.engine_key, e)

    async def _say(self, kind: str, text: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(kind, text)
        except Exception as e:  # noqa: BLE001
            log.error("notify failed: %s", e)

    def _spawn(self, name: str, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("executor task %s failed", name)
                if self.on_error is not None:
                    self.on_error(f"executor:{name}", e)

        task = asyncio.create_task(guarded(), name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _quote(self, symbol: str, fallback: tuple[float, float]) -> tuple[float, float]:
        try:
            q = self.quote_fn(symbol)
        except Exception as e:  # noqa: BLE001
            log.debug("quote_fn failed for %s: %s", symbol, e)
            q = None
        if q and q[0] and q[1] and q[1] >= q[0] > 0:
            return float(q[0]), float(q[1])
        return fallback

    def caps_check(self, contracts: int, ask: float) -> tuple[bool, str]:
        """Tier 1 order sanity caps, re-checked at the order with the live ask (never trust the sizing alone)."""
        t1 = self.policy.tier1
        max_c = t1.max_contracts_per_1000 * max(1, int(self.account // 1000))
        max_prem = t1.max_premium_frac_of_account * self.account
        if contracts <= 0:
            return False, "no contracts"
        if contracts > max_c:
            return False, f"Tier 1 cap: {contracts} contracts > {max_c} allowed per ${self.account:,.0f}"
        if contracts * ask * 100.0 > max_prem + 1e-9:
            return False, f"Tier 1 cap: premium ${contracts * ask * 100.0:,.0f} > {t1.max_premium_frac_of_account:.0%} of the account (${max_prem:,.0f})"
        return True, ""

    # ------------------------------------------------------------------------------------------- engine events
    async def on_engine_events(self, events: list[dict[str, Any]], *, feed_mode: str, lag_s: float | None = None) -> None:
        """Called once per engine tick with the tick's events. Never blocks on orders or approvals (tasks are spawned)."""
        self.feed_mode = feed_mode
        for ev in events:
            kind = ev.get("type")
            if kind == "open":
                if ev.get("source") != "gate":
                    continue                      # fast-lane hypotheses never trade
                self.counts["events"] += 1
                await self._on_open(ev, feed_mode, lag_s)
            elif kind == "close":
                self.counts["events"] += 1
                await self._on_close(ev)
            elif kind == "bank":
                self.counts["events"] += 1
                await self._on_bank(ev)

    async def _on_open(self, ev: dict[str, Any], feed_mode: str, lag_s: float | None) -> None:
        row = ev["row"]
        key = ev["position"]
        if key in self.trades:
            return
        now = self.clock.now()
        sizing = row.get("sizing") or {}
        t = PaperTrade(engine_key=key, trade_date=self.trade_date, symbol=row["symbol"], option_symbol=row["option_symbol"],
                       occ=streamer_to_occ(row["option_symbol"]), direction=row["direction"], option_type=row["option_type"], window=row.get("window", ""),
                       lane=row.get("lane", ""), contracts=int(row.get("contracts") or 0), account=self.account,
                       shadow_entry_bid=float(row.get("entry_bid") or 0.0), shadow_entry_ask=float(row.get("entry_premium") or 0.0), created_at=now,
                       sizing_mode=str(sizing.get("mode", "")), probability=row.get("probability"))
        self.trades[key] = t
        if self.halted:
            t.status, t.block_reason = "blocked", f"halt: {(self.killswitch.info() or {}).get('reason', 'kill switch engaged')}"
        elif self.policy.require_realtime and feed_mode != "realtime":
            t.status, t.block_reason = "blocked", f"feed_not_realtime: mode {feed_mode}, lag {lag_s}s — no paper orders on a delayed feed (D19)"
        else:
            ok, why = self.caps_check(t.contracts, t.shadow_entry_ask)
            if not ok:
                t.status, t.block_reason = "refused", why
        if t.status in ("blocked", "refused"):
            self.counts[t.status] += 1
            t.note(t.block_reason)
            self._persist(t)
            log.warning("paper entry %s %s: %s", key, t.status, t.block_reason)
            if t.status == "refused":
                await self._say("alert", f"PAPER refused ▸ {t.symbol} {t.option_symbol} ×{t.contracts}: {t.block_reason}")
            return
        t.status = "proposed"
        self._persist(t)
        self._spawn(f"enter:{key}", self._enter(t, row))

    async def _on_close(self, ev: dict[str, Any]) -> None:
        t = self.trades.get(ev["position"])
        if t is None:
            return
        row = ev.get("row") or {}
        t.shadow_exit_bid = row.get("exit_premium")
        t.shadow_r = row.get("r_result")
        reason = str(ev.get("reason") or "close")
        if t.status == "proposed":
            await self.approvals.expire(t.engine_key, f"position closed by the engine ({reason}) before a decision")
        elif t.status == "working" and t._entry_ticket is not None:
            t._close_pending = reason
            await self.orders.cancel(t._entry_ticket, f"engine closed the position ({reason})")
        elif t.status == "open" and not self.halted:
            self._spawn(f"exit:{t.engine_key}", self._exit(t, reason))
        elif t.status == "closing":
            t._close_pending = reason
        self._persist(t)

    async def _on_bank(self, ev: dict[str, Any]) -> None:
        t = self.trades.get(ev["position"])
        if t is None or t.status != "open" or self.halted:
            return
        n = min(int(ev.get("contracts") or 0), t.remaining)
        if n <= 0:
            return
        self.counts["banks"] += 1
        self._spawn(f"bank:{t.engine_key}", self._exit(t, "bank", contracts=n, kind="bank"))

    # ----------------------------------------------------------------------------------------------- entering
    def _proposal_text(self, t: PaperTrade, row: dict[str, Any]) -> str:
        sizing = row.get("sizing") or {}
        risk = t.contracts * t.shadow_entry_ask * 100.0
        mid = (t.shadow_entry_bid + t.shadow_entry_ask) / 2.0
        gates = row.get("gates") or {}
        stop = row.get("window_end")
        stop_s = ""
        if stop:
            try:
                stop_s = f" · stop {et(datetime.fromisoformat(str(stop).replace('Z', '+00:00'))):%H:%M}"
            except ValueError:
                stop_s = ""
        p = f" · p={t.probability}" if t.probability is not None else ""
        g = f" · {gates.get('passed', 6)}/6 gates" if gates else ""
        return (f"Proposed ▸ {t.symbol} {t.direction} {t.option_type} {t.option_symbol} ×{t.contracts} @ ask {t.shadow_entry_ask:.2f} (mid {mid:.2f})"
                f" · R ${risk:.0f} ({sizing.get('mode', 'floor')}) · {t.window}{g}{p}{stop_s}\n"
                f"Paper order in the sandbox ({self.broker.account_masked}): limit at mid, retry ladder to the ask.")

    async def _enter(self, t: PaperTrade, row: dict[str, Any]) -> None:
        prop = await self.approvals.propose(t.engine_key, t.symbol, self._proposal_text(t, row), contracts=t.contracts, option_symbol=t.option_symbol)
        t.proposal_id = prop.proposal_id
        self.counts["proposed"] += 1
        self._persist(t)
        status = await self.approvals.wait(prop)
        t.decision, t.decided_at, t.decision_latency_s = status, prop.decided_at, prop.latency_s
        if status != "approved":
            t.status = status if status in ("skipped", "timeout", "expired", "failed") else "skipped"
            self.counts[t.status] += 1
            t.note(prop.note)
            self._persist(t)
            log.info("paper entry %s not placed: %s (%s)", t.engine_key, t.status, prop.note)
            return
        self.counts["approved"] += 1
        if self.halted:
            t.status, t.block_reason = "halted", "approved but the kill switch is engaged"
            t.note(t.block_reason)
            self._persist(t)
            return
        if t._close_pending:
            t.status = "expired"
            t.note(f"approved after the engine closed the position ({t._close_pending}) — nothing placed")
            self.counts["expired"] += 1
            self._persist(t)
            return
        bid, ask = self._quote(t.option_symbol, (t.shadow_entry_bid, t.shadow_entry_ask))
        ok, why = self.caps_check(t.contracts, ask)
        if not ok:
            t.status, t.block_reason = "refused", why
            self.counts["refused"] += 1
            t.note(why)
            self._persist(t)
            await self._say("alert", f"PAPER refused ▸ {t.symbol} {t.option_symbol} ×{t.contracts}: {why}")
            return
        ticket = Ticket.new(t.engine_key, t.option_symbol, "buy", t.contracts, bid, ask, self.clock.now(), kind="entry")
        t._entry_ticket = ticket
        t.status, t.entry = "working", ticket.row()
        self._persist(t)
        done = await self.orders.work(ticket, quote_fn=lambda: self.quote_fn(t.option_symbol))
        t.entry = done.row()
        t.fees += float(getattr(done, "fees", 0.0) or 0.0)
        if done.filled_quantity > 0:
            t.entry_qty, t.entry_price, t.entry_at = done.filled_quantity, done.avg_fill_price, done.done_at
            t.slippage_entry = round((done.avg_fill_price or 0.0) - t.shadow_entry_ask, 4)
            t.status = "open"
            self.counts["filled" if done.status == "filled" else "partial"] += 1
            if done.status == "partial":
                t.note(f"partial entry {done.filled_quantity}/{t.contracts}")
            self._persist(t)
            await self._say("alert", f"PAPER open ▸ {t.symbol} {t.option_symbol} ×{t.entry_qty} filled @ {t.entry_price:.2f}"
                                     f" (engine ask {t.shadow_entry_ask:.2f}, slip {t.slippage_entry:+.2f}) · ladder {', '.join(str(x) for x in done.limit_prices)}")
            if self.halted:
                t.note("filled during a halt — flattening")
                self._persist(t)
                await self._flatten_trade(t, "halt")
            elif t._close_pending:
                reason, t._close_pending = t._close_pending, None
                await self._exit(t, reason)
        else:
            t.status = done.status if done.status in ("unfilled", "rejected", "error", "cancelled") else "unfilled"
            self.counts[t.status if t.status in self.counts else "unfilled"] = self.counts.get(t.status, 0) + 1
            t.note(done.reason)
            self._persist(t)
            await self._say("alert", f"PAPER not filled ▸ {t.symbol} {t.option_symbol} ×{t.contracts}: {done.status} — {done.reason}")
        t._entry_ticket = None

    # ------------------------------------------------------------------------------------------------ exiting
    def _book_exit(self, t: PaperTrade, done: Ticket, reason: str) -> None:
        t.exits.append(done.row())
        t.fees += float(getattr(done, "fees", 0.0) or 0.0)
        if done.filled_quantity > 0:
            prev_q, prev_px = t.exit_qty, t.exit_price or 0.0
            t.exit_qty = prev_q + done.filled_quantity
            t.exit_price = round((prev_q * prev_px + done.filled_quantity * (done.avg_fill_price or 0.0)) / t.exit_qty, 4)
            t.exit_at = done.done_at
        if t.remaining == 0 and t.entry_qty > 0:
            proceeds = t.exit_qty * (t.exit_price or 0.0) * 100.0
            t.realized_pnl = round(proceeds - t.cost - t.fees, 2)
            t.realized_r = round(t.realized_pnl / t.cost, 4) if t.cost > 0 else None
            if t.shadow_exit_bid is not None and t.exit_price is not None:
                t.slippage_exit = round(t.exit_price - float(t.shadow_exit_bid), 4)
            t.exit_reason = reason
            t.status = "closed"
            self.counts["closed"] += 1

    async def _exit(self, t: PaperTrade, reason: str, *, contracts: int | None = None, kind: str = "exit") -> None:
        qty = t.remaining if contracts is None else min(contracts, t.remaining)
        if qty <= 0 or t.status not in ("open", "closing"):
            return
        t.status = "closing"
        self._persist(t)
        bid, ask = self._quote(t.option_symbol, (float(t.shadow_exit_bid or t.shadow_entry_bid), float(t.shadow_entry_ask)))
        ticket = Ticket.new(t.engine_key, t.option_symbol, "sell", qty, bid, ask, self.clock.now(), kind=kind, seq=len(t.exits) + 1)
        t._exit_ticket = ticket
        done = await self.orders.work(ticket, quote_fn=lambda: self.quote_fn(t.option_symbol))
        t._exit_ticket = None
        self._book_exit(t, done, reason)
        if done.filled_quantity < qty and not self.halted:
            # an exit must complete: escalate the remainder to the flatten ladder (bid → bid − step → market)
            left = qty - done.filled_quantity
            t.note(f"exit ladder {done.status} ({done.reason}); flattening {left} at market")
            self._persist(t)
            fl = Ticket.new(t.engine_key, t.option_symbol, "sell", left, bid, ask, self.clock.now(), kind="flatten", seq=len(t.exits) + 1)
            t._exit_ticket = fl
            done2 = await self.orders.flatten(fl, quote_fn=lambda: self.quote_fn(t.option_symbol))
            t._exit_ticket = None
            self._book_exit(t, done2, reason)
        if t.status == "closed":
            self._persist(t)
            await self._say("alert", f"PAPER close ▸ {t.symbol} {t.option_symbol} ×{t.exit_qty} @ {t.exit_price:.2f} → {t.realized_r:+.2f}R (${t.realized_pnl:+,.0f})"
                                     f" · {reason} · shadow {t.shadow_r:+.2f}R" if t.shadow_r is not None else
                                     f"PAPER close ▸ {t.symbol} {t.option_symbol} ×{t.exit_qty} @ {t.exit_price:.2f} → {t.realized_r:+.2f}R (${t.realized_pnl:+,.0f}) · {reason}")
        else:
            if t.remaining > 0:
                t.status = "open"
                if kind != "bank":
                    t.note(f"{t.remaining} contract(s) still open after the exit attempt ({reason})")
                    await self._say("system", f"PAPER ⚠ {t.symbol} {t.option_symbol}: {t.remaining} contract(s) still open after the exit ({reason})")
            self._persist(t)
            if t._close_pending and t.remaining > 0 and not self.halted:
                r, t._close_pending = t._close_pending, None
                await self._exit(t, r)

    async def _flatten_trade(self, t: PaperTrade, reason: str) -> None:
        if t._flattening or t.remaining <= 0:
            return
        t._flattening = True
        try:
            t.status = "closing"
            self._persist(t)
            bid, ask = self._quote(t.option_symbol, (float(t.shadow_exit_bid or t.shadow_entry_bid), float(t.shadow_entry_ask)))
            fl = Ticket.new(t.engine_key, t.option_symbol, "sell", t.remaining, bid, ask, self.clock.now(), kind="flatten", seq=len(t.exits) + 1)
            t._exit_ticket = fl
            done = await self.orders.flatten(fl, quote_fn=lambda: self.quote_fn(t.option_symbol))
            t._exit_ticket = None
            self._book_exit(t, done, reason)
            if t.status != "closed":
                t.status = "open"
                t.note(f"NOT FLAT after the flatten ladder: {done.status} — {done.reason}")
            self._persist(t)
        finally:
            t._flattening = False

    # ----------------------------------------------------------------------------------------------------- halt
    async def halt(self, reason: str, by: str) -> dict[str, Any]:
        """Engage the kill switch and get flat. Returns {seconds, closed, cancelled, unknown_flattened, ok}."""
        now = self.clock.now()
        start = now.timestamp()
        fresh = self.killswitch.engage(reason, by, now)
        if self.set_halt_setting is not None:
            try:
                self.set_halt_setting(True)
            except Exception as e:  # noqa: BLE001
                log.warning("halt setting sync failed: %s", e)
        self.halting = True
        self.counts["halts"] += 1
        cancelled: list[str] = []
        closed: list[str] = []
        unknown: list[str] = []
        try:
            await self.approvals.expire("*", f"halt by {by}: {reason}")
            for t in self.trades.values():
                if t.status == "working" and t._entry_ticket is not None:
                    await self.orders.cancel(t._entry_ticket, f"halt by {by}")
                    cancelled.append(t._entry_ticket.ticket_id)
                if t.status == "closing" and t._exit_ticket is not None and t._exit_ticket.kind != "flatten":
                    await self.orders.cancel(t._exit_ticket, f"halt by {by}")
                    cancelled.append(t._exit_ticket.ticket_id)
            # positions the book does not know (manual sandbox trades, a previous run): flatten them too
            try:
                known = {t.occ for t in self.trades.values()}
                for bp in await self.broker.positions():
                    if bp.quantity > 0 and bp.symbol not in known:
                        unknown.append(bp.symbol)
                        sym = occ_to_streamer(bp.symbol) if bp.symbol.strip() else bp.symbol
                        bid, ask = self._quote(sym, (bp.mark or bp.average_open_price, bp.mark or bp.average_open_price))
                        fl = Ticket.new(f"unknown|{bp.symbol.strip()}", sym, "sell", int(bp.quantity), bid, ask, self.clock.now(), kind="flatten")
                        self._spawn(f"flatten-unknown:{bp.symbol.strip()}", self.orders.flatten(fl, quote_fn=lambda s=sym: self.quote_fn(s)))
            except BrokerError as e:
                log.error("halt: could not read broker positions: %s", e)
            # flatten everything open; keep looping until the book is flat or the budget is spent
            deadline = start + self.policy.halt_max_wait_s
            while True:
                for t in self.trades.values():
                    if t.status == "open" and t.remaining > 0 and not t._flattening:
                        self._spawn(f"flatten:{t.engine_key}", self._flatten_trade(t, f"halt by {by}"))
                busy = [t for t in self.trades.values() if t.status in ("working", "closing") or (t.status == "open" and t.remaining > 0)]
                busy_tasks = [x for x in self.tasks if not x.done() and x.get_name().startswith(("flatten", "exit", "enter", "bank"))]
                if not busy and not busy_tasks:
                    break
                if self.clock.now().timestamp() >= deadline:
                    log.error("halt: still not flat after %.0fs: %s", self.policy.halt_max_wait_s, [t.engine_key for t in busy])
                    break
                await self.clock.sleep(0.5)
            closed = [t.engine_key for t in self.trades.values() if t.status == "closed" and t.exit_reason and t.exit_reason.startswith("halt")]
        finally:
            self.halting = False
        seconds = round(self.clock.now().timestamp() - start, 1)
        flat = not [t for t in self.trades.values() if t.status in ("working", "closing") or (t.status == "open" and t.remaining > 0)]
        self.counts["flattened"] += len(closed)
        rec = {"at": now.isoformat(), "by": by, "reason": reason, "seconds": seconds, "closed": closed, "cancelled": cancelled, "unknown_flattened": unknown,
               "ok": flat, "fresh": fresh, "within_budget": seconds <= self.policy.halt_budget_s}
        self.halt_events.append(rec)
        try:
            self.store.log_event(self.clock.now(), "WARNING", "halt", f"halt by {by}: {reason} → flat={flat} in {seconds}s, closed {len(closed)}, cancelled {len(cancelled)}")
        except Exception:  # noqa: BLE001
            pass
        await self._say("system", f"HALT ▸ by {by} ({reason}): {'flat' if flat else 'NOT FLAT'} in {seconds}s · {len(closed)} position(s) closed · "
                                  f"{len(cancelled)} order(s) cancelled" + (f" · {len(unknown)} unknown position(s) flattened" if unknown else "")
                                  + " · no new entries until /resume")
        return rec

    async def resume(self, by: str) -> bool:
        ok = self.killswitch.clear(by)
        if self.set_halt_setting is not None:
            try:
                self.set_halt_setting(False)
            except Exception as e:  # noqa: BLE001
                log.warning("halt setting sync failed: %s", e)
        if ok:
            await self._say("system", f"RESUME ▸ kill switch cleared by {by}; entries allowed again (approval mode)")
        return ok

    # ------------------------------------------------------------------------------------------- reconciliation
    async def reconcile(self) -> dict[str, Any]:
        now = self.clock.now()
        self.counts["reconciliations"] += 1
        try:
            broker_pos = await self.broker.positions()
            live = await self.broker.live_orders()
        except BrokerError as e:
            rec = {"ts": now.isoformat(), "ok": False, "error": f"broker unreachable: {e}"}
            self.last_reconcile = rec
            try:
                self.store.insert_reconciliation(now, self.trade_date.isoformat(), self.run_id, False, rec)
            except Exception:  # noqa: BLE001
                pass
            return rec
        local: dict[str, int] = {}
        for t in self.trades.values():
            if t.remaining > 0 and t.status in ("open", "closing"):
                local[t.occ] = local.get(t.occ, 0) + t.remaining
        working = {}
        for t in self.trades.values():
            for tk in (t._entry_ticket, t._exit_ticket):
                if tk is not None and tk.broker_order_ids:
                    working[tk.broker_order_ids[-1]] = tk.ticket_id
        mismatches: list[dict[str, Any]] = []
        bpos = {bp.symbol: bp for bp in broker_pos if bp.quantity != 0}
        for occ, bp in sorted(bpos.items()):
            if bp.quantity != local.get(occ, 0):
                mismatches.append({"symbol": occ, "kind": "position", "broker": bp.quantity, "local": local.get(occ, 0)})
        for occ, q in sorted(local.items()):
            if occ not in bpos and q > 0:
                mismatches.append({"symbol": occ, "kind": "position", "broker": 0, "local": q})
        for o in live:
            if o.order_id not in working:
                mismatches.append({"symbol": o.symbol, "kind": "unknown_live_order", "order_id": o.order_id, "status": o.status, "price": o.price})
        ok = not mismatches
        rec = {"ts": now.isoformat(), "ok": ok, "broker_positions": [bp.as_dict() for bp in broker_pos], "local_positions": local,
               "live_orders": [{"order_id": o.order_id, "symbol": o.symbol, "status": o.status, "price": o.price, "ticket": working.get(o.order_id)} for o in live],
               "mismatches": mismatches, "open_trades": sum(1 for t in self.trades.values() if t.remaining > 0 and t.status in ("open", "closing"))}
        self.last_reconcile = rec
        try:
            self.store.insert_reconciliation(now, self.trade_date.isoformat(), self.run_id, ok, rec)
        except Exception as e:  # noqa: BLE001
            log.error("reconciliation persist failed: %s", e)
        if mismatches:
            self.counts["mismatches"] += 1
            keys = {f"{m['kind']}:{m['symbol']}:{m.get('broker')}/{m.get('local')}/{m.get('order_id')}" for m in mismatches}
            new = keys - self._alerted_mismatches
            if new:
                self._alerted_mismatches |= keys
                await self._say("system", "RECONCILE ⚠ broker ≠ book: " + "; ".join(
                    f"{m['symbol'].strip()} {m['kind']} broker {m.get('broker')} / book {m.get('local')}" if m["kind"] == "position"
                    else f"{m['symbol'].strip()} unknown live order {m.get('order_id')} ({m.get('status')})" for m in mismatches[:5]))
        return rec

    # ----------------------------------------------------------------------------------------------- lifecycle
    async def end_of_day(self, reason: str = "session end") -> None:
        """Final tick: expire pending proposals, cancel working entries, try to close anything still open."""
        await self.approvals.expire("*", reason)
        for t in list(self.trades.values()):
            if t.status == "working" and t._entry_ticket is not None:
                await self.orders.cancel(t._entry_ticket, reason)
        for _ in range(60):
            if not [x for x in self.tasks if not x.done() and x.get_name().startswith("enter")]:
                break
            await self.clock.sleep(0.5)
        for t in list(self.trades.values()):
            if t.status == "open" and t.remaining > 0 and not self.halted:
                await self._exit(t, reason)
        still = [t for t in self.trades.values() if t.remaining > 0 and t.status in ("open", "closing")]
        if still:
            await self._say("system", "PAPER ⚠ still open at session end: " + ", ".join(f"{t.symbol} {t.option_symbol} ×{t.remaining}" for t in still))

    async def shutdown(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    # ------------------------------------------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        trades = list(self.trades.values())
        closed = [t for t in trades if t.status == "closed" and t.realized_r is not None]
        return {
            "mode": self.policy.mode, "broker": getattr(self.broker, "name", "?"), "account_masked": getattr(self.broker, "account_masked", "?"),
            "halted": self.halted, "halt_info": self.killswitch.info(), "feed_mode": self.feed_mode, "require_realtime": self.policy.require_realtime,
            "counts": dict(self.counts), "approvals": self.approvals.summary(), "trades": len(trades),
            "open": sum(1 for t in trades if t.remaining > 0 and t.status in ("open", "closing")),
            "closed": len(closed), "realized_r": round(sum(t.realized_r or 0.0 for t in closed), 4), "realized_pnl": round(sum(t.realized_pnl or 0.0 for t in closed), 2),
            "by_status": {s: sum(1 for t in trades if t.status == s) for s in sorted({t.status for t in trades})},
            "last_reconcile": {"ts": self.last_reconcile.get("ts"), "ok": self.last_reconcile.get("ok"), "mismatches": len(self.last_reconcile.get("mismatches") or [])}
            if self.last_reconcile else None,
            "halt_events": list(self.halt_events), "tickets": len(self.orders.tickets),
        }


__all__ = ["Executor", "ExecutionPolicy", "PaperTrade", "ALLOW_AUTO_MODE"]
