"""The order manager: one *ticket* = one logical fill request worked as a **limit-at-mid retry ladder**.

Entry/exit ladder (plan: "limit at mid, retry ladder"): place at the mid rounded toward the far side, re-price every
`step_seconds` a step closer to the far side, the last step *at* the far side (the ask for a buy, the bid for a sell),
wait `fill_wait_seconds` more, then cancel. A partially filled order is never replaced (tastytrade would reject it);
it rests until the ladder's end and is then cancelled, leaving a `partial` ticket the executor closes out.

Flatten ladder (the kill switch): the bid at once, bid minus a step at +3 s, **market** at +6 s. A stubborn book is
flat well inside the spec's 10 seconds; a normal one fills on the first order.

Every placement, replacement, fill, cancel and rejection is logged to SQLite (`paper_orders`, mirrored to
`saa.paper_orders`). All waiting goes through the injected clock, so the whole ladder runs on the virtual clock in tests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from ..clock import Clock, et
from ..store import Store
from .broker import Broker, BrokerError, BrokerOrder
from .symbols import nearest_tick, round_to_tick, streamer_to_occ, tick_size

log = logging.getLogger("saa.orders")

QuoteFn = Callable[[], tuple[float, float] | None]


@dataclass(frozen=True)
class LadderPolicy:
    step_seconds: float = 5.0          # time at each rung
    steps: int = 3                     # re-prices after the initial mid (the last rung is the far side)
    fill_wait_seconds: float = 5.0     # grace at the far side before cancelling
    poll_seconds: float = 1.0          # order-status poll
    flatten_step_seconds: float = 3.0  # kill switch: bid → bid − step at 3 s → market at 6 s
    flatten_step_frac: float = 0.05
    flatten_market_after_s: float = 6.0
    flatten_max_seconds: float = 30.0  # after a market order, how long to keep polling before declaring it stuck


def ladder_prices(side: str, bid: float, ask: float, *, steps: int, symbol: str | None = None) -> list[float]:
    """Rung prices from the mid to the far side, on the class's tick grid (`symbol`), strictly monotone, no duplicates."""
    bid, ask = float(bid), float(ask)
    far = ask if side == "buy" else bid
    mid = (bid + ask) / 2.0
    out = [round_to_tick(mid, side, symbol)]         # the mid, rounded toward the far side
    for i in range(1, steps + 1):
        frac = i / steps
        out.append(round_to_tick(far, side, symbol) if i == steps else nearest_tick(mid + (far - mid) * frac, symbol))
    # clamp to the far side and drop rungs that do not move the price
    cleaned: list[float] = []
    for p in out:
        p = min(p, far) if side == "buy" else max(p, far)
        p = round(p, 2)
        if not cleaned or (p > cleaned[-1] if side == "buy" else p < cleaned[-1]):
            cleaned.append(p)
    return cleaned


@dataclass
class Ticket:
    ticket_id: str
    engine_key: str
    symbol: str                   # streamer symbol (.SPY260928C654)
    occ: str                      # OCC symbol (SPY   260928C00654000)
    side: str                     # buy | sell
    action: str                   # buy_to_open | sell_to_close
    quantity: int
    mark_bid: float
    mark_ask: float
    created_at: datetime
    kind: str = "entry"           # entry | exit | bank | flatten
    status: str = "new"           # new | working | filled | partial | unfilled | cancelled | rejected | error
    limit_prices: list[float | None] = field(default_factory=list)   # None = market
    broker_order_ids: list[str] = field(default_factory=list)
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    done_at: datetime | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
    fees: float = 0.0
    fees_by_order: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)   # the broker's warnings on this ticket's orders (e.g. queued for the next session)

    @classmethod
    def new(cls, engine_key: str, symbol: str, side: str, quantity: int, bid: float, ask: float, now: datetime, *, kind: str = "entry",
            seq: int = 0) -> "Ticket":
        action = "buy_to_open" if side == "buy" else "sell_to_close"
        tid = f"{engine_key}|{kind}|{et(now):%H%M%S}" + (f"|{seq}" if seq else "")
        return cls(tid, engine_key, symbol, streamer_to_occ(symbol), side, action, int(quantity), float(bid), float(ask), now, kind=kind)

    @property
    def done(self) -> bool:
        return self.status in ("filled", "partial", "unfilled", "cancelled", "rejected", "error")

    @property
    def notional(self) -> float | None:
        return None if self.avg_fill_price is None else self.filled_quantity * self.avg_fill_price * 100.0

    def row(self) -> dict[str, Any]:
        return {"ticket_id": self.ticket_id, "engine_key": self.engine_key, "symbol": self.symbol, "occ_symbol": self.occ, "side": self.side,
                "action": self.action, "quantity": self.quantity, "kind": self.kind, "status": self.status, "mark_bid": self.mark_bid,
                "mark_ask": self.mark_ask, "limit_prices": list(self.limit_prices), "broker_order_ids": list(self.broker_order_ids),
                "filled_quantity": self.filled_quantity, "avg_fill_price": self.avg_fill_price, "fills": list(self.fills), "reason": self.reason,
                "created_at": self.created_at.isoformat(), "done_at": self.done_at.isoformat() if self.done_at else None,
                "cancel_requested": self.cancel_requested, "fees": self.fees, "warnings": list(self.warnings)}


class OrderManager:
    def __init__(self, broker: Broker, store: Store, clock: Clock, policy: LadderPolicy | None = None, *, run_id: str = "", trade_date: date | None = None):
        self.broker = broker
        self.store = store
        self.clock = clock
        self.policy = policy or LadderPolicy()
        self.run_id = run_id
        self.trade_date = trade_date
        self.tickets: dict[str, Ticket] = {}
        self.on_update: Callable[[Ticket], None] | None = None

    # -------------------------------------------------------------------------------------------- bookkeeping
    def _persist(self, t: Ticket) -> None:
        self.tickets[t.ticket_id] = t
        td = (self.trade_date or et(t.created_at).date()).isoformat()
        try:
            self.store.upsert_paper_order(t.row(), td, self.run_id, self.clock.now())
        except Exception as e:  # noqa: BLE001 — logging must never break the order path
            log.error("paper_orders persist failed for %s: %s", t.ticket_id, e)
        if self.on_update is not None:
            try:
                self.on_update(t)
            except Exception as e:  # noqa: BLE001
                log.error("ticket on_update failed: %s", e)

    def _absorb(self, t: Ticket, o: BrokerOrder) -> None:
        """Copy the broker's view of the current order into the ticket (fills are keyed by fill id, never double counted)."""
        known = {f["fill_id"] for f in t.fills}
        for f in o.fills:
            if f.fill_id not in known:
                t.fills.append({"fill_id": f.fill_id, "order_id": o.order_id, "quantity": f.quantity, "price": f.price, "at": f.at.isoformat()})
        qty = sum(f["quantity"] for f in t.fills)
        t.filled_quantity = qty
        t.avg_fill_price = round(sum(f["quantity"] * f["price"] for f in t.fills) / qty, 4) if qty else None
        if o.fees is not None and o.filled_quantity:
            t.fees_by_order[o.order_id] = float(o.fees)
            t.fees = round(sum(t.fees_by_order.values()), 4)
        for w in getattr(o, "warnings", None) or []:
            if w not in t.warnings:
                t.warnings.append(w)

    def _finish(self, t: Ticket, status: str, reason: str) -> Ticket:
        t.status, t.reason, t.done_at = status, reason, self.clock.now()
        self._persist(t)
        log.info("ticket %s %s: %s (%d/%d @ %s)", t.ticket_id, status, reason, t.filled_quantity, t.quantity, t.avg_fill_price)
        return t

    async def cancel(self, t: Ticket, reason: str) -> None:
        """Ask a working ticket to stop (the ladder loop cancels the live order on its next poll)."""
        t.cancel_requested, t.cancel_reason = True, reason

    # ------------------------------------------------------------------------------------------------ ladder
    async def work(self, t: Ticket, quote_fn: QuoteFn | None = None) -> Ticket:
        """Run the limit-at-mid ladder to completion. Returns the same ticket, now terminal."""
        p = self.policy

        def quote() -> tuple[float, float]:
            q = quote_fn() if quote_fn is not None else None
            return (float(q[0]), float(q[1])) if q and q[0] and q[1] and q[1] >= q[0] else (t.mark_bid, t.mark_ask)

        bid, ask = quote()
        rungs = ladder_prices(t.side, bid, ask, steps=p.steps, symbol=t.occ)
        price = rungs[0]
        try:
            o = await self.broker.place(t.occ, t.action, t.quantity, price, external_id=t.ticket_id)
        except BrokerError as e:
            return self._finish(t, "error", f"BrokerError on place: {e}")
        t.broker_order_ids.append(o.order_id)
        t.limit_prices.append(price)
        t.status = "working"
        self._persist(t)
        step = 0
        rung_until = self.clock.now().timestamp() + p.step_seconds
        while True:
            self._absorb(t, o)
            if o.status == "rejected":
                return self._finish(t, "rejected", f"rejected: {o.reject_reason or 'no reason given'}")
            if o.status == "filled" or t.filled_quantity >= t.quantity:
                return self._finish(t, "filled", f"filled on rung {step} @ {t.avg_fill_price}")
            if o.status in ("cancelled", "expired", "replaced"):
                return self._finish(t, "partial" if t.filled_quantity else "cancelled", f"order {o.status} outside the ladder")
            if t.cancel_requested:
                try:
                    o = await self.broker.cancel(o.order_id)
                except BrokerError as e:
                    log.warning("cancel of %s failed (%s); re-reading", o.order_id, e)
                    o = await self._safe_get(o)
                self._absorb(t, o)
                if o.status == "filled":
                    return self._finish(t, "filled", f"filled before the cancel took ({t.cancel_reason})")
                return self._finish(t, "partial" if t.filled_quantity else "cancelled", t.cancel_reason)
            now_s = self.clock.now().timestamp()
            if now_s >= rung_until - 1e-6:
                if step < len(rungs) - 1 and t.filled_quantity == 0:
                    step += 1
                    bid, ask = quote()
                    live_rungs = ladder_prices(t.side, bid, ask, steps=p.steps, symbol=t.occ)
                    price = live_rungs[min(step, len(live_rungs) - 1)]
                    far = ask if t.side == "buy" else bid
                    if step >= len(rungs) - 1:
                        price = round_to_tick(far, t.side, t.occ)
                    rungs = live_rungs if len(live_rungs) > step else rungs
                    if price != t.limit_prices[-1]:
                        try:
                            o = await self.broker.replace(o.order_id, price)
                        except BrokerError as e:
                            log.warning("replace of %s failed (%s); keeping the resting order", o.order_id, e)
                            o = await self._safe_get(o)
                        else:
                            t.broker_order_ids.append(o.order_id)
                            t.limit_prices.append(price)
                            self._persist(t)
                            self._absorb(t, o)
                            if o.status == "filled":
                                return self._finish(t, "filled", f"filled on rung {step} @ {t.avg_fill_price}")
                    rung_until = now_s + (p.fill_wait_seconds if step >= len(rungs) - 1 else p.step_seconds)
                else:
                    try:
                        o = await self.broker.cancel(o.order_id)
                    except BrokerError as e:
                        log.warning("cancel of %s failed (%s); re-reading", o.order_id, e)
                        o = await self._safe_get(o)
                    self._absorb(t, o)
                    if o.status == "filled":
                        return self._finish(t, "filled", f"filled at the end of the ladder @ {t.avg_fill_price}")
                    if t.filled_quantity:
                        return self._finish(t, "partial", f"partial {t.filled_quantity}/{t.quantity} — unfilled remainder cancelled after the ladder")
                    return self._finish(t, "unfilled", f"unfilled after the ladder ({', '.join(str(x) for x in t.limit_prices)})")
            await self.clock.sleep(p.poll_seconds)
            o = await self._safe_get(o)

    async def _safe_get(self, o: BrokerOrder) -> BrokerOrder:
        try:
            return await self.broker.get_order(o.order_id)
        except BrokerError as e:
            log.warning("get_order %s failed: %s", o.order_id, e)
            return o

    # ----------------------------------------------------------------------------------------------- flatten
    async def flatten(self, t: Ticket, quote_fn: QuoteFn | None = None) -> Ticket:
        """Kill-switch exit: bid now, bid − step at +3 s, market at +6 s. Polls until filled (or `flatten_max_seconds`)."""
        p = self.policy
        q = quote_fn() if quote_fn is not None else None
        bid, ask = (float(q[0]), float(q[1])) if q and q[0] and q[1] else (t.mark_bid, t.mark_ask)
        t.kind = "flatten"
        start = self.clock.now().timestamp()
        price: float | None = round_to_tick(bid, "sell", t.occ)
        try:
            o = await self.broker.place(t.occ, t.action, t.quantity, price, external_id=t.ticket_id)
        except BrokerError as e:
            log.error("flatten place failed (%s); trying a market order", e)
            try:
                o = await self.broker.place(t.occ, t.action, t.quantity, None, external_id=t.ticket_id)
                price = None
            except BrokerError as e2:
                return self._finish(t, "error", f"BrokerError on flatten: {e2}")
        t.broker_order_ids.append(o.order_id)
        t.limit_prices.append(price)
        t.status = "working"
        self._persist(t)
        stage = 0
        while True:
            self._absorb(t, o)
            if o.status == "rejected":
                return self._finish(t, "rejected", f"flatten rejected: {o.reject_reason}")
            if o.status == "filled" or t.filled_quantity >= t.quantity:
                return self._finish(t, "filled", f"flat in {self.clock.now().timestamp() - start:.0f}s @ {t.avg_fill_price}")
            elapsed = self.clock.now().timestamp() - start
            want: float | None
            if stage == 0 and elapsed >= p.flatten_step_seconds:
                step = max(tick_size(bid, t.occ), bid * p.flatten_step_frac)
                want, stage = round_to_tick(bid - step, "sell", t.occ), 1
            elif stage == 1 and elapsed >= p.flatten_market_after_s:
                want, stage = None, 2
            elif elapsed >= p.flatten_max_seconds:
                return self._finish(t, "partial" if t.filled_quantity else "unfilled", f"NOT FLAT after {elapsed:.0f}s — order {o.order_id} {o.status}")
            else:
                want = price
            if want != price and t.filled_quantity == 0:
                try:
                    o = await self.broker.replace(o.order_id, want)
                    price = want
                    t.broker_order_ids.append(o.order_id)
                    t.limit_prices.append(price)
                    self._persist(t)
                    continue
                except BrokerError as e:
                    log.warning("flatten replace failed (%s)", e)
            await self.clock.sleep(p.poll_seconds)
            o = await self._safe_get(o)


__all__ = ["LadderPolicy", "OrderManager", "Ticket", "ladder_prices"]
