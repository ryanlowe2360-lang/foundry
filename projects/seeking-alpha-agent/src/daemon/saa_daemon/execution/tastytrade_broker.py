"""The tastytrade implementation of the Broker interface — the **sandbox** account only until M5.

Maps the SDK's order model (`LimitOrder` / `MarketOrder` with one equity-option `Leg`, `PlacedOrder` with per-leg fills,
`CurrentPosition`) onto `BrokerOrder` / `BrokerPosition`. Prices: tastytrade signs the price by effect (negative =
debit, positive = credit); callers of this class always pass a positive per-share price and the action decides the sign.
The SDK is imported inside the methods so the rest of the package stays importable without it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from .broker import BrokerError, BrokerFill, BrokerOrder, BrokerPosition, error_text

log = logging.getLogger("saa.tt_broker")

STATUS_MAP = {
    "Received": "live", "Routed": "live", "In Flight": "live", "Live": "live", "Contingent": "live",
    "Cancel Requested": "live", "Replace Requested": "live",
    "Filled": "filled", "Cancelled": "cancelled", "Removed": "cancelled", "Partially Removed": "cancelled",
    "Rejected": "rejected", "Expired": "expired",
}
ACTIONS = {"buy_to_open": "Buy to Open", "sell_to_close": "Sell to Close"}   # long premium only in v1 — nothing else is ever sent
RETRY_429_S: tuple[float, ...] = (1.0, 2.0)                                     # backoff after a gateway 429, then give up


def _is_429(e: BaseException) -> bool:
    text = str(e)
    return "429" in text and ("Too Many Requests" in text or "Couldn't parse response" in text)


def _warning_text(w: Any) -> str:
    code, msg = getattr(w, "code", None), getattr(w, "message", None)
    return f"{code}: {msg}" if code and msg else str(w)


def _mask(acct: str | None) -> str:
    return f"…{acct[-4:]}" if acct and len(acct) > 4 else "…"


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _msg(e: BaseException) -> str:
    return error_text(e)


class TastytradeBroker:
    name = "tastytrade-sandbox"

    def __init__(self, session: Any, account: Any, *, env: str = "sandbox"):
        if env != "sandbox":
            raise BrokerError("production brokerage is M5 — the executor only talks to the sandbox account")
        self.session = session
        self.account = account
        self.env = env
        self.account_masked = _mask(getattr(account, "account_number", None))
        self.calls = 0
        self.retries = 0
        self.last_error: str | None = None

    @classmethod
    async def open(cls, session: Any, *, env: str = "sandbox", account_number: str | None = None) -> "TastytradeBroker":
        from tastytrade import Account

        accts = await Account.get(session)
        accts = accts if isinstance(accts, list) else [accts]
        if account_number:
            accts = [a for a in accts if a.account_number == account_number]
        if not accts:
            raise BrokerError("no account on the sandbox session")
        return cls(session, accts[0], env=env)

    # ------------------------------------------------------------------------------------------------ mapping
    def _leg(self, symbol: str, action: str, quantity: int) -> Any:
        from tastytrade.order import InstrumentType, Leg, OrderAction

        if action not in ACTIONS:
            raise BrokerError(f"action {action!r} is not allowed (long premium only: buy_to_open / sell_to_close)")
        return Leg(instrument_type=InstrumentType.EQUITY_OPTION, symbol=symbol, action=OrderAction(ACTIONS[action]), quantity=int(quantity))

    def _order(self, leg: Any, action: str, price: float | None) -> Any:
        from tastytrade.order import LimitOrder, MarketOrder, OrderTimeInForce

        if price is None:
            return MarketOrder(time_in_force=OrderTimeInForce.DAY, legs=[leg])
        signed = Decimal(f"{abs(float(price)):.2f}")
        if action.startswith("buy"):
            signed = -signed                                   # debit
        return LimitOrder(time_in_force=OrderTimeInForce.DAY, legs=[leg], price=signed)

    @staticmethod
    def _convert(po: Any, *, fees: float | None = None) -> BrokerOrder:
        legs = list(getattr(po, "legs", None) or [])
        leg = legs[0] if legs else None
        fills: list[BrokerFill] = []
        for f in (getattr(leg, "fills", None) or []):
            at = getattr(f, "filled_at", None) or datetime.now(timezone.utc)
            fills.append(BrokerFill(str(getattr(f, "fill_id", "")), int(getattr(f, "quantity", 0) or 0), float(getattr(f, "fill_price", 0) or 0), at))
        raw_status = str(getattr(po, "status", "") or "")
        status = STATUS_MAP.get(raw_status, "live")
        price = _f(getattr(po, "price", None))
        order_type = "market" if str(getattr(po, "order_type", "")) == "Market" else "limit"
        leg_qty = int(getattr(leg, "quantity", 0) or 0) if leg is not None else 0
        if status == "filled" and not fills and leg_qty > 0:
            # `Filled` with no fill rows on the leg (a simulated fill may not list them): the quantity must still be
            # accounted for, or the order path would treat a held position as "nothing filled" and never close it.
            # One *reported* fill at the order's own limit price; a market order's price is unknown here → 0.0.
            px = abs(price) if (price is not None and order_type == "limit") else 0.0
            at = getattr(po, "updated_at", None) or datetime.now(timezone.utc)
            fills.append(BrokerFill(f"{getattr(po, 'id', '')}:reported", leg_qty, px, at))
            log.warning("order %s is Filled without fill rows — booked %d @ %.2f as reported", getattr(po, "id", "?"), leg_qty, px)
        qty = sum(x.quantity for x in fills)
        avg = round(sum(x.quantity * x.price for x in fills) / qty, 4) if qty else None
        if status == "cancelled" and leg is not None and qty and qty >= leg_qty:
            status = "filled"
        action_raw = str(getattr(leg, "action", "") or "")
        action = {v: k for k, v in ACTIONS.items()}.get(action_raw, action_raw.lower().replace(" ", "_"))
        return BrokerOrder(order_id=str(getattr(po, "id", "")), symbol=str(getattr(leg, "symbol", "") or ""), action=action,
                           quantity=leg_qty, order_type=order_type,
                           price=abs(price) if (price is not None and order_type == "limit") else None, status=status, filled_quantity=qty, avg_fill_price=avg, fills=fills,
                           reject_reason=getattr(po, "reject_reason", None), external_id=getattr(po, "external_identifier", None),
                           updated_at=getattr(po, "updated_at", None), fees=fees)

    async def _call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """One API call with a short retry on the gateway's 429 (the sandbox rate-limits bursts; the error arrives as an
        HTML page the SDK cannot parse). Everything else propagates to the caller."""
        from tastytrade.utils import TastytradeError

        for attempt, delay in enumerate(RETRY_429_S + (None,)):
            self.calls += 1
            try:
                return await fn()
            except TastytradeError as e:
                if delay is None or not _is_429(e):
                    raise
                log.warning("429 from the broker API (attempt %d); retrying in %ss", attempt + 1, delay)
                self.retries += 1
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _guard(self, what: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        from tastytrade.utils import TastytradeError

        try:
            return await self._call(fn)
        except TastytradeError as e:
            self.last_error = f"{what}: {_msg(e)}"
            raise BrokerError(self.last_error) from e
        except BrokerError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{what}: {type(e).__name__}: {_msg(e)}"
            raise BrokerError(self.last_error) from e

    async def dry_run(self, symbol: str, action: str, quantity: int, price: float | None) -> str | None:
        """Ask the sandbox to validate (not place) an order: None when it would be accepted, else the refusal text.
        The paper self-tests use it to settle on an instrument the sandbox actually trades before any real placement."""
        from tastytrade.utils import TastytradeError

        try:
            order = self._order(self._leg(symbol, action, quantity), action, price)
            resp = await self._call(lambda: self.account.place_order(self.session, order, dry_run=True))
        except (TastytradeError, BrokerError) as e:
            return _msg(e)
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {_msg(e)}"
        errors = list(getattr(resp, "errors", None) or [])
        return "; ".join(str(e) for e in errors)[:300] if errors else None

    # ---------------------------------------------------------------------------------------------- interface
    async def place(self, symbol: str, action: str, quantity: int, price: float | None, *, external_id: str | None = None) -> BrokerOrder:
        leg = self._leg(symbol, action, quantity)
        order = self._order(leg, action, price)
        if external_id:
            order.external_identifier = external_id[:64]
        resp = await self._guard("place", lambda: self.account.place_order(self.session, order, dry_run=False))
        fees = _f(getattr(getattr(resp, "fee_calculation", None), "total_fees", None))
        fees = abs(fees) if fees is not None else None         # the SDK signs fees by effect (debit = negative)
        warnings = [_warning_text(w) for w in (getattr(resp, "warnings", None) or [])]
        if warnings:
            log.info("order warnings: %s", "; ".join(warnings)[:300])
        o = self._convert(resp.order, fees=fees)
        o.warnings = warnings
        if getattr(resp, "errors", None) and o.status != "rejected":
            o.status, o.reject_reason = "rejected", "; ".join(str(e) for e in resp.errors)[:300]
        return o

    async def replace(self, order_id: str, price: float | None) -> BrokerOrder:
        old = await self.get_order(order_id)
        leg = self._leg(old.symbol, old.action, old.quantity - old.filled_quantity if old.filled_quantity else old.quantity)
        new = self._order(leg, old.action, price)
        placed = await self._guard("replace", lambda: self.account.replace_order(self.session, int(order_id), new))
        return self._convert(placed)

    async def cancel(self, order_id: str) -> BrokerOrder:
        await self._guard("cancel", lambda: self.account.delete_order(self.session, int(order_id)))
        return await self.get_order(order_id)

    async def get_order(self, order_id: str) -> BrokerOrder:
        po = await self._guard("get_order", lambda: self.account.get_order(self.session, int(order_id)))
        return self._convert(po)

    async def live_orders(self) -> list[BrokerOrder]:
        rows = await self._guard("live_orders", lambda: self.account.get_live_orders(self.session))
        out = [self._convert(po) for po in rows]
        return [o for o in out if not o.terminal]

    async def positions(self) -> list[BrokerPosition]:
        from tastytrade.order import InstrumentType

        rows = await self._guard("positions", lambda: self.account.get_positions(self.session, instrument_type=InstrumentType.EQUITY_OPTION))
        out = []
        for p in rows:
            q = int(getattr(p, "quantity", 0) or 0)
            if str(getattr(p, "quantity_direction", "Long")).lower().startswith("short"):
                q = -q
            if q == 0:
                continue
            out.append(BrokerPosition(str(p.symbol), q, float(getattr(p, "average_open_price", 0) or 0), int(getattr(p, "multiplier", 100) or 100),
                                      underlying=getattr(p, "underlying_symbol", None), mark=_f(getattr(p, "mark_price", None))))
        return out

    async def balances(self) -> dict[str, Any]:
        b = await self._guard("balances", lambda: self.account.get_balances(self.session))
        keys = ("cash_balance", "net_liquidating_value", "equity_buying_power", "derivative_buying_power", "day_trading_buying_power",
                "maintenance_requirement", "cash_available_to_withdraw")
        return {k: _f(getattr(b, k, None)) for k in keys if getattr(b, k, None) is not None}


__all__ = ["TastytradeBroker", "STATUS_MAP", "ACTIONS"]
