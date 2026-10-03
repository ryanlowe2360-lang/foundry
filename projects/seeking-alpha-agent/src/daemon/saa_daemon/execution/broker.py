"""The broker boundary: one small interface the order path talks to, a FakeBroker that implements it for tests and
dry runs, and (in `tastytrade_broker.py`) the sandbox implementation on the tastytrade SDK.

Every price here is per share (an option quoted 1.16 costs $116 per contract). Actions are the tastytrade words in
snake case: `buy_to_open`, `sell_to_close` (long premium only in v1 — `sell_to_open` is never issued, see the executor).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


def error_text(e: BaseException | str, limit: int = 200) -> str:
    """An API/SDK error on one line: the SDK joins `code: message` pairs with trailing newlines. Only line breaks are
    collapsed — spaces inside the text stay, because OCC symbols carry meaningful padding (`SPY   261005C00770000`)."""
    text = e if isinstance(e, str) else str(e)
    return " ".join(line.strip() for line in text.splitlines() if line.strip())[:limit]


class BrokerError(Exception):
    """Transport / API failure. The message never contains credentials."""


@dataclass
class BrokerFill:
    fill_id: str
    quantity: int
    price: float
    at: datetime


@dataclass
class BrokerOrder:
    order_id: str
    symbol: str                        # OCC symbol
    action: str                        # buy_to_open | sell_to_close
    quantity: int
    order_type: str                    # limit | market
    price: float | None                # limit price per share; None for market
    status: str                        # received | live | filled | cancelled | rejected | expired | replaced
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    fills: list[BrokerFill] = field(default_factory=list)
    reject_reason: str | None = None
    external_id: str | None = None
    updated_at: datetime | None = None
    fees: float | None = None          # total fees the broker quoted for this order (sandbox: usually 0)

    @property
    def side(self) -> str:
        return "sell" if self.action.startswith("sell") else "buy"

    @property
    def terminal(self) -> bool:
        return self.status in ("filled", "cancelled", "rejected", "expired", "replaced")

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fills"] = [{"fill_id": f.fill_id, "quantity": f.quantity, "price": f.price, "at": f.at.isoformat()} for f in self.fills]
        d["updated_at"] = self.updated_at.isoformat() if self.updated_at else None
        return d


@dataclass
class BrokerPosition:
    symbol: str                        # OCC symbol
    quantity: int                      # signed: + long
    average_open_price: float
    multiplier: int = 100
    underlying: str | None = None
    mark: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Broker(Protocol):
    """What the order manager, the executor and the reconciler need. Implementations: FakeBroker, TastytradeBroker."""

    name: str
    account_masked: str

    async def place(self, symbol: str, action: str, quantity: int, price: float | None, *, external_id: str | None = None) -> BrokerOrder: ...
    async def replace(self, order_id: str, price: float | None) -> BrokerOrder: ...
    async def cancel(self, order_id: str) -> BrokerOrder: ...
    async def get_order(self, order_id: str) -> BrokerOrder: ...
    async def live_orders(self) -> list[BrokerOrder]: ...
    async def positions(self) -> list[BrokerPosition]: ...
    async def balances(self) -> dict[str, Any]: ...


# ----------------------------------------------------------------------------------------------------------- fake
class _QuoteBook(dict):
    """`quotes` for the FakeBroker: explicit entries first, then an optional live source (OCC symbol → (bid, ask))."""

    def __init__(self, quote_fn: Any = None):
        super().__init__()
        self.quote_fn = quote_fn

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if self.quote_fn is not None:
            q = self.quote_fn(key)
            if q and q[0] and q[1]:
                return (float(q[0]), float(q[1]))
        return default


class FakeBroker:
    """A deterministic exchange in memory.

    `quotes[occ] = (bid, ask)` is the market. Fill modes:
      * ``market`` (default) — a limit fills when it is marketable (buy limit ≥ ask fills at the ask; sell limit ≤ bid at
        the bid); resting orders are re-checked against the current quotes on every call, so a quote move can fill them.
      * ``at_limit`` — every limit fills immediately at its limit price (what the tastytrade sandbox does).
      * ``never`` — limits never fill (market orders still do when ``market_fills``).
      * ``partial`` — half the quantity (rounded up) fills at the limit at once, the rest never.
      * ``reject`` — every placement is rejected with ``reject_reason``.
    ``fail_next`` lists method names whose next call raises :class:`BrokerError` (transport failures).
    ``untradable`` is the set of OCC symbols this broker's instrument universe does not know: ``dry_run`` refuses them
    and ``place`` raises — the tastytrade sandbox's ``instrument_validation_failed`` behaviour.
    """

    name = "fake"

    def __init__(self, *, mode: str = "market", reject_reason: str = "rejected by fake broker", market_fills: bool = True,
                 fail_next: list[str] | None = None, account_masked: str = "…0000", cash: float = 1000.0,
                 quote_fn: Any = None, untradable: set[str] | None = None):
        assert mode in ("market", "at_limit", "never", "partial", "reject")
        self.mode = mode
        self.reject_reason = reject_reason
        self.market_fills = market_fills
        self.fail_next = list(fail_next or [])
        self.untradable: set[str] = set(untradable or ())
        self.account_masked = account_masked
        self.cash = cash
        self.quotes: dict[str, tuple[float, float]] = _QuoteBook(quote_fn)
        self.orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, list[float]] = {}     # occ → [quantity, average price]
        self.calls: list[tuple[Any, ...]] = []
        self._ids = itertools.count(1)
        self._fill_ids = itertools.count(1)
        self.now_fn = lambda: datetime.now(timezone.utc)
        self.fill_delay_polls = 0                         # marketable orders fill after this many get_order() calls
        self._polls: dict[str, int] = {}

    # ----------------------------------------------------------------------------------------------- internals
    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_next:
            self.fail_next.remove(method)
            raise BrokerError(f"{method}: simulated transport failure")

    def _fill(self, o: BrokerOrder, qty: int, price: float) -> None:
        price = round(price, 2)
        o.fills.append(BrokerFill(f"f{next(self._fill_ids)}", qty, price, self.now_fn()))
        total = sum(f.quantity * f.price for f in o.fills)
        o.filled_quantity += qty
        o.avg_fill_price = round(total / o.filled_quantity, 4)
        o.status = "filled" if o.filled_quantity >= o.quantity else "live"
        o.updated_at = self.now_fn()
        pos = self._positions.setdefault(o.symbol, [0, 0.0])
        if o.side == "buy":
            new_q = pos[0] + qty
            pos[1] = (pos[0] * pos[1] + qty * price) / new_q if new_q else 0.0
            pos[0] = new_q
            self.cash -= qty * price * 100.0
        else:
            pos[0] -= qty
            self.cash += qty * price * 100.0
        if pos[0] == 0:
            self._positions.pop(o.symbol, None)

    def _evaluate(self, o: BrokerOrder) -> None:
        if o.terminal or o.status == "rejected":
            return
        remaining = o.quantity - o.filled_quantity
        if remaining <= 0:
            return
        q = self.quotes.get(o.symbol)
        if o.order_type == "market":
            if self.market_fills and q:
                self._fill(o, remaining, q[1] if o.side == "buy" else q[0])
            return
        if self.mode == "never":
            return
        if self.mode == "at_limit":
            self._fill(o, remaining, float(o.price or 0.0))
            return
        if self.mode == "partial":
            if o.filled_quantity == 0:
                self._fill(o, max(1, math.ceil(o.quantity / 2)), float(o.price or 0.0))
            return
        # market mode: marketable → fill at the touch
        if not q or o.price is None:
            return
        bid, ask = q
        if o.side == "buy" and o.price >= ask:
            self._fill(o, remaining, ask)
        elif o.side == "sell" and o.price <= bid:
            self._fill(o, remaining, bid)

    def _new(self, symbol: str, action: str, quantity: int, price: float | None, external_id: str | None) -> BrokerOrder:
        oid = f"fk{next(self._ids)}"
        o = BrokerOrder(oid, symbol, action, int(quantity), "market" if price is None else "limit", None if price is None else round(float(price), 2),
                        "live", external_id=external_id, updated_at=self.now_fn())
        self.orders[oid] = o
        if self.mode == "reject":
            o.status, o.reject_reason = "rejected", self.reject_reason
            return o
        if self.fill_delay_polls and price is not None:
            self._polls[oid] = self.fill_delay_polls
        else:
            self._evaluate(o)
        return o

    # ------------------------------------------------------------------------------------------------ interface
    async def dry_run(self, symbol: str, action: str, quantity: int, price: float | None) -> str | None:
        """Mirror of TastytradeBroker.dry_run: None = the order would be accepted, else the refusal text."""
        self.calls.append(("dry_run", symbol, action, quantity, price))
        if symbol in self.untradable:
            return f"instrument_validation_failed: Trading of {symbol} is not supported"
        return self.reject_reason if self.mode == "reject" else None

    async def place(self, symbol: str, action: str, quantity: int, price: float | None, *, external_id: str | None = None) -> BrokerOrder:
        self.calls.append(("place", symbol, action, quantity, price))
        self._maybe_fail("place")
        if quantity <= 0:
            raise BrokerError("quantity must be positive")
        if symbol in self.untradable:
            raise BrokerError(f"place: instrument_validation_failed: Trading of {symbol} is not supported")
        return self._new(symbol, action, quantity, price, external_id)

    async def replace(self, order_id: str, price: float | None) -> BrokerOrder:
        self.calls.append(("replace", order_id, price))
        self._maybe_fail("replace")
        old = self.orders[order_id]
        if old.terminal:
            raise BrokerError(f"order {order_id} is {old.status}; cannot replace")
        if old.filled_quantity:
            raise BrokerError(f"order {order_id} is partially filled; cannot replace")
        old.status, old.updated_at = "replaced", self.now_fn()
        return self._new(old.symbol, old.action, old.quantity, price, old.external_id)

    async def cancel(self, order_id: str) -> BrokerOrder:
        self.calls.append(("cancel", order_id))
        self._maybe_fail("cancel")
        o = self.orders[order_id]
        if not o.terminal:
            o.status, o.updated_at = "cancelled", self.now_fn()
        return o

    async def get_order(self, order_id: str) -> BrokerOrder:
        self.calls.append(("get_order", order_id))
        self._maybe_fail("get_order")
        o = self.orders[order_id]
        if order_id in self._polls:
            self._polls[order_id] -= 1
            if self._polls[order_id] <= 0:
                del self._polls[order_id]
                self._evaluate(o)
        else:
            self._evaluate(o)
        return o

    async def live_orders(self) -> list[BrokerOrder]:
        self.calls.append(("live_orders",))
        self._maybe_fail("live_orders")
        for o in self.orders.values():
            self._evaluate(o)
        return [o for o in self.orders.values() if not o.terminal]

    async def positions(self) -> list[BrokerPosition]:
        self.calls.append(("positions",))
        self._maybe_fail("positions")
        for o in self.orders.values():
            self._evaluate(o)
        out = []
        for sym, (q, px) in sorted(self._positions.items()):
            mark = self.quotes.get(sym)
            out.append(BrokerPosition(sym, int(q), round(px, 4), underlying=sym[:6].strip(), mark=(mark[0] + mark[1]) / 2 if mark else None))
        return out

    async def balances(self) -> dict[str, Any]:
        self.calls.append(("balances",))
        self._maybe_fail("balances")
        return {"cash": round(self.cash, 2), "net_liq": round(self.cash + sum(q * px * 100 for q, px in self._positions.values()), 2)}


__all__ = ["Broker", "BrokerError", "BrokerFill", "BrokerOrder", "BrokerPosition", "FakeBroker"]
