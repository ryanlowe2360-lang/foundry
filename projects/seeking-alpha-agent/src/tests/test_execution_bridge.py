"""The two places M4 touches the outside world, offline: the tastytrade SDK order model (LimitOrder/Leg/PlacedOrder/
CurrentPosition shapes of SDK 13.x → BrokerOrder/BrokerPosition) and the Telegram Bot API (sendMessage with inline
buttons, editMessageText, getUpdates long-poll, callback + command dispatch, foreign chats ignored, 409 back-off)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from saa_daemon import config
from saa_daemon.clock import FakeClock, et_dt
from saa_daemon.execution.broker import BrokerError
from saa_daemon.execution.tastytrade_broker import TastytradeBroker
from saa_daemon.execution.telegram_bot import TelegramBot, TelegramConflict

D = date(2026, 9, 28)
OCC = "SPY   260928C00654000"


# ------------------------------------------------------------------------------------------------------ SDK shapes
def _placed(oid: int, status: str, *, qty: int = 1, fills: list[tuple[int, str]] | None = None, price: str = "-1.14", action: str = "Buy to Open",
            reject: str | None = None, order_type: str = "Limit") -> dict:
    """A PlacedOrder document as the API returns it (dasherized keys, signed price via price-effect)."""
    leg = {"instrument-type": "Equity Option", "symbol": OCC, "action": action, "quantity": qty, "remaining-quantity": qty - sum(q for q, _ in (fills or [])),
           "fills": [{"fill-id": f"f{i}", "quantity": q, "fill-price": px, "filled-at": "2026-09-28T13:37:05.000Z"} for i, (q, px) in enumerate(fills or [])]}
    p = Decimal(price)
    return {"id": oid, "account-number": "5WX09103", "time-in-force": "Day", "order-type": order_type, "size": qty, "underlying-symbol": "SPY",
            "underlying-instrument-type": "Equity", "status": status, "cancellable": status == "Live", "editable": status == "Live", "edited": False,
            "updated-at": "2026-09-28T13:37:05.000Z", "legs": [leg], "price": str(abs(p)), "price-effect": "Debit" if p < 0 else "Credit",
            "reject-reason": reject, "external-identifier": "k1|entry|093702"}


class FakeSession:
    pass


class FakeAccount:
    """Mimics tastytrade.Account's order/position methods with SDK response objects built from API-shaped dicts."""

    account_number = "5WX09103"

    def __init__(self) -> None:
        from tastytrade.order import PlacedOrder
        self.PlacedOrder = PlacedOrder
        self.placed: list = []
        self.replaced: list[tuple[int, object]] = []
        self.deleted: list[int] = []
        self.orders: dict[int, dict] = {}
        self.fail = False
        self.next_id = 1001
        self.dry_runs: list = []
        self.untradable: set[str] = set()

    def _resp(self, doc: dict, fees: str = "0.0"):
        from tastytrade.order import PlacedOrderResponse
        return PlacedOrderResponse[self.PlacedOrder](**{
            "buying-power-effect": {"change-in-margin-requirement": "114", "change-in-margin-requirement-effect": "Debit", "change-in-buying-power": "114",
                                    "change-in-buying-power-effect": "Debit", "current-buying-power": "1000", "current-buying-power-effect": "Credit",
                                    "new-buying-power": "886", "new-buying-power-effect": "Credit", "isolated-order-margin-requirement": "114",
                                    "isolated-order-margin-requirement-effect": "Debit", "is-spread": False, "impact": "114", "effect": "Debit"},
            "order": doc, "fee-calculation": {"regulatory-fees": "0.0", "regulatory-fees-effect": "None", "clearing-fees": "0.0", "clearing-fees-effect": "None",
                                              "commission": fees, "commission-effect": "Debit", "proprietary-index-option-fees": "0.0",
                                              "proprietary-index-option-fees-effect": "None", "total-fees": fees, "total-fees-effect": "Debit"},
            "warnings": [{"code": "paper", "message": "sandbox order"}]})

    async def place_order(self, session, order, dry_run=True):
        from tastytrade.utils import TastytradeError
        if self.fail:
            raise TastytradeError("Error: 422 — order rejected by risk")
        body = json.loads(order.model_dump_json(exclude_none=True, by_alias=True))
        if body["legs"][0]["symbol"] in self.untradable:          # what the cert environment says for an instrument it does not know
            raise TastytradeError(f"instrument_validation_failed: Trading of {body['legs'][0]['symbol']} is not supported\n")
        if dry_run:
            self.dry_runs.append(order)
            return self._resp(_placed(0, "Received", qty=int(body["legs"][0]["quantity"]), price=body.get("price", "0"), action=body["legs"][0]["action"],
                                      order_type=body["order-type"]), fees="1.00")
        self.placed.append(order)
        oid = self.next_id
        self.next_id += 1
        body = json.loads(order.model_dump_json(exclude_none=True, by_alias=True))
        leg = body["legs"][0]
        is_market = body["order-type"] == "Market"
        px = "0" if is_market else body["price"]
        signed = ("-" if body.get("price-effect") == "Debit" else "") + str(px) if not is_market else "0"
        doc = _placed(oid, "Filled" if is_market else "Live", qty=int(leg["quantity"]), fills=[(int(leg["quantity"]), "1.12")] if is_market else None,
                      price=signed, action=leg["action"], order_type=body["order-type"])
        doc["external-identifier"] = body.get("external-identifier")
        self.orders[oid] = doc
        return self._resp(doc, fees="1.00")

    async def get_order(self, session, order_id: int):
        return self.PlacedOrder(**self.orders[order_id])

    async def replace_order(self, session, old_order_id: int, new_order):
        self.replaced.append((old_order_id, new_order))
        self.orders[old_order_id]["status"] = "Cancelled"
        body = json.loads(new_order.model_dump_json(exclude_none=True, by_alias=True))
        oid = self.next_id
        self.next_id += 1
        doc = _placed(oid, "Filled", fills=[(1, body["price"])], price=("-" if body.get("price-effect") == "Debit" else "") + body["price"])
        self.orders[oid] = doc
        return self.PlacedOrder(**doc)

    async def delete_order(self, session, order_id: int):
        self.deleted.append(order_id)
        self.orders[order_id]["status"] = "Cancelled"

    async def get_live_orders(self, session):
        return [self.PlacedOrder(**d) for d in self.orders.values() if d["status"] == "Live"]

    async def get_positions(self, session, instrument_type=None):
        from tastytrade.account import CurrentPosition
        doc = {"account-number": "5WX09103", "symbol": OCC, "instrument-type": "Equity Option", "underlying-symbol": "SPY", "quantity": 1,
               "quantity-direction": "Long", "close-price": "1.20", "average-open-price": "1.12", "multiplier": 100, "cost-effect": "Debit",
               "is-suppressed": False, "is-frozen": False, "realized-day-gain": "0", "realized-day-gain-effect": "None", "realized-today": "0",
               "realized-today-effect": "None", "created-at": "2026-09-28T13:37:05.000Z", "updated-at": "2026-09-28T13:37:05.000Z", "mark-price": "1.21"}
        return [CurrentPosition(**doc)]

    async def get_balances(self, session):
        from tastytrade.account import AccountBalance
        doc = {"account-number": "5WX09103", "cash-balance": "886.0", "long-equity-value": "0", "short-equity-value": "0", "long-derivative-value": "112",
               "short-derivative-value": "0", "long-futures-value": "0", "short-futures-value": "0", "long-futures-derivative-value": "0",
               "short-futures-derivative-value": "0", "long-margineable-value": "0", "short-margineable-value": "0", "margin-equity": "998",
               "equity-buying-power": "886", "derivative-buying-power": "886", "day-trading-buying-power": "0", "futures-margin-requirement": "0",
               "available-trading-funds": "886", "maintenance-requirement": "0", "maintenance-call-value": "0", "reg-t-call-value": "0",
               "day-trading-call-value": "0", "day-equity-call-value": "0", "net-liquidating-value": "998", "cash-available-to-withdraw": "886",
               "day-trade-excess": "0", "pending-cash": "0", "pending-cash-effect": "None", "long-cryptocurrency-value": "0",
               "short-cryptocurrency-value": "0", "cryptocurrency-margin-requirement": "0", "unsettled-cryptocurrency-fiat-amount": "0",
               "unsettled-cryptocurrency-fiat-effect": "None", "closed-loop-available-balance": "0", "equity-offering-margin-requirement": "0",
               "long-bond-value": "0", "bond-margin-requirement": "0", "used-derivative-buying-power": "114", "snapshot-date": "2026-09-28",
               "reg-t-margin-requirement": "0", "futures-overnight-margin-requirement": "0", "futures-intraday-margin-requirement": "0",
               "maintenance-excess": "886", "pending-margin-interest": "0", "effective-cryptocurrency-buying-power": "0", "updated-at": "2026-09-28T13:37:05.000Z"}
        try:
            return AccountBalance(**doc)
        except Exception:  # pragma: no cover - the SDK's balance model may require more fields; the broker only reads what exists
            class _B:  # noqa: D401
                cash_balance = Decimal("886.0")
                net_liquidating_value = Decimal("998")
            return _B()


@pytest.mark.asyncio
async def test_sdk_order_round_trip_and_mapping():
    acct = FakeAccount()
    b = TastytradeBroker(FakeSession(), acct)
    assert b.account_masked == "…9103"
    o = await b.place(OCC, "buy_to_open", 1, 1.14, external_id="k1|entry|093702")
    sent = json.loads(acct.placed[0].model_dump_json(exclude_none=True, by_alias=True))
    assert sent["order-type"] == "Limit" and sent["time-in-force"] == "Day" and sent["price"] == "1.14" and sent["price-effect"] == "Debit"
    assert sent["legs"] == [{"instrument-type": "Equity Option", "symbol": OCC, "action": "Buy to Open", "quantity": 1}]
    assert sent["external-identifier"] == "k1|entry|093702"
    assert o.order_id == "1001" and o.status == "live" and o.price == 1.14 and o.action == "buy_to_open" and o.quantity == 1 and o.fees == 1.0
    # replace → a new id, filled at the new price
    o2 = await b.replace("1001", 1.16)
    assert o2.order_id == "1002" and o2.status == "filled" and o2.filled_quantity == 1 and o2.avg_fill_price == 1.16 and o2.fills[0].fill_id == "f0"
    rep = json.loads(acct.replaced[0][1].model_dump_json(exclude_none=True, by_alias=True))
    assert rep["price"] == "1.16" and rep["price-effect"] == "Debit"
    assert (await b.get_order("1001")).status == "cancelled"
    # a sell to close carries a credit price; a market order has no price
    s = await b.place(OCC, "sell_to_close", 1, 1.30)
    body = json.loads(acct.placed[-1].model_dump_json(exclude_none=True, by_alias=True))
    assert body["price-effect"] == "Credit" and body["price"] == "1.30" and s.action == "sell_to_close"
    m = await b.place(OCC, "sell_to_close", 1, None)
    body = json.loads(acct.placed[-1].model_dump_json(exclude_none=True, by_alias=True))
    assert body["order-type"] == "Market" and "price" not in body and m.order_type == "market" and m.status == "filled" and m.price is None
    # cancel + live orders + positions + balances
    await b.cancel("1001")
    assert acct.deleted == [1001]
    live = await b.live_orders()
    assert [x.order_id for x in live] == ["1003"]
    pos = await b.positions()
    assert len(pos) == 1 and pos[0].symbol == OCC and pos[0].quantity == 1 and pos[0].average_open_price == 1.12 and pos[0].mark == 1.21 and pos[0].underlying == "SPY"
    bal = await b.balances()
    assert bal.get("cash_balance") == 886.0 and bal.get("net_liquidating_value") == 998.0
    # a dry run validates the exact order without placing it: None = accepted, else the refusal on one line (no raise)
    n_placed = len(acct.placed)
    assert await b.dry_run(OCC, "buy_to_open", 1, 1.14) is None and len(acct.dry_runs) == 1 and len(acct.placed) == n_placed
    dr = json.loads(acct.dry_runs[0].model_dump_json(exclude_none=True, by_alias=True))
    assert dr["price"] == "1.14" and dr["price-effect"] == "Debit" and dr["legs"][0]["symbol"] == OCC
    acct.untradable.add(OCC)
    assert await b.dry_run(OCC, "buy_to_open", 1, 1.14) == f"instrument_validation_failed: Trading of {OCC} is not supported"
    with pytest.raises(BrokerError) as ei:
        await b.place(OCC, "buy_to_open", 1, 1.14)
    assert str(ei.value) == f"place: instrument_validation_failed: Trading of {OCC} is not supported" and "\n" not in str(ei.value)
    acct.untradable.clear()
    assert await b.dry_run(OCC, "sell_to_open", 1, 1.14) == "action 'sell_to_open' is not allowed (long premium only: buy_to_open / sell_to_close)"
    # the SDK's error becomes a BrokerError; the sell-to-open action is refused before any call
    acct.fail = True
    assert await b.dry_run(OCC, "buy_to_open", 1, 1.14) == "Error: 422 — order rejected by risk"
    with pytest.raises(BrokerError, match="422"):
        await b.place(OCC, "buy_to_open", 1, 1.14)
    with pytest.raises(BrokerError, match="long premium only"):
        await b.place(OCC, "sell_to_open", 1, 1.14)
    with pytest.raises(BrokerError, match="M5"):
        TastytradeBroker(FakeSession(), acct, env="prod")


def test_placed_order_status_mapping():
    from tastytrade.order import PlacedOrder
    conv = TastytradeBroker._convert
    assert conv(PlacedOrder(**_placed(1, "Received"))).status == "live"
    assert conv(PlacedOrder(**_placed(2, "Rejected", reject="Account does not have permission for this order"))).reject_reason.startswith("Account does not")
    assert conv(PlacedOrder(**_placed(2, "Rejected", reject="x"))).status == "rejected"
    o = conv(PlacedOrder(**_placed(3, "Cancelled", qty=2, fills=[(1, "1.10")])))
    assert o.status == "cancelled" and o.filled_quantity == 1 and o.avg_fill_price == 1.10          # partial then cancelled
    o = conv(PlacedOrder(**_placed(4, "Filled", qty=2, fills=[(1, "1.10"), (1, "1.12")])))
    assert o.status == "filled" and o.filled_quantity == 2 and o.avg_fill_price == 1.11
    assert conv(PlacedOrder(**_placed(5, "Expired"))).status == "expired"
    assert conv(PlacedOrder(**_placed(6, "Cancel Requested"))).status == "live" and not conv(PlacedOrder(**_placed(6, "Cancel Requested"))).terminal


# ------------------------------------------------------------------------------------------------------- telegram
class TgHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[list[dict]] = []
        self.conflict_once = False
        self.msg_id = 500

    async def post_json(self, url, body, *, headers=None, timeout=15.0):
        method = url.rsplit("/", 1)[-1]
        assert "bot123456:telegram-bot-token-value/" in url
        self.calls.append((method, body))
        if method == "sendMessage":
            self.msg_id += 1
            return 200, {"ok": True, "result": {"message_id": self.msg_id}}
        if method == "editMessageText":
            if "same" in body["text"]:
                return 400, {"ok": False, "description": "Bad Request: message is not modified"}
            return 200, {"ok": True, "result": True}
        if method == "answerCallbackQuery":
            return 200, {"ok": True, "result": True}
        if method == "getUpdates":
            if self.conflict_once:
                self.conflict_once = False
                return 409, {"ok": False, "description": "Conflict: terminated by other getUpdates request"}
            batch = self.updates.pop(0) if self.updates else []
            return 200, {"ok": True, "result": batch}
        return 404, {"ok": False, "description": "unknown"}

    async def aclose(self):
        pass


def _settings(env_file: Path):
    return config.load_settings(env_file, environ={})


@pytest.mark.asyncio
async def test_send_with_buttons_edit_and_answer(env_file: Path):
    http = TgHttp()
    bot = TelegramBot(_settings(env_file), http, FakeClock(et_dt(D, time(9, 37))))
    assert bot.configured and bot.chat_id == "987654321"
    mid = await bot.send("Proposed ▸ SPY", buttons=[("✅ Approve", "appr:abc:ok"), ("⏭ Skip", "appr:abc:skip")])
    assert mid == 501
    m, body = http.calls[-1]
    assert m == "sendMessage" and body["chat_id"] == "987654321" and body["reply_markup"] == {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "appr:abc:ok"},
                                                                                                                      {"text": "⏭ Skip", "callback_data": "appr:abc:skip"}]]}
    await bot.edit(501, "Proposed ▸ SPY\n✅ Approved")
    assert http.calls[-1][0] == "editMessageText" and http.calls[-1][1]["message_id"] == 501
    await bot.edit(501, "same text")                                    # "not modified" is not an error
    assert bot.sent == 1 and bot.edited == 1


@pytest.mark.asyncio
async def test_poll_dispatches_callbacks_and_commands_and_keeps_the_offset(env_file: Path):
    http = TgHttp()
    clock = FakeClock(et_dt(D, time(9, 37)))
    bot = TelegramBot(_settings(env_file), http, clock)
    http.conflict_once = True
    http.updates = [
        [{"update_id": 10, "callback_query": {"id": "cb1", "from": {"id": 987654321, "first_name": "Ryan"}, "data": "appr:abc:ok",
                                              "message": {"message_id": 501, "chat": {"id": 987654321}}}},
         {"update_id": 11, "callback_query": {"id": "cb2", "from": {"id": 42, "first_name": "Mallory"}, "data": "appr:abc:ok",
                                              "message": {"message_id": 7, "chat": {"id": 42}}}},
         {"update_id": 12, "message": {"message_id": 502, "chat": {"id": 987654321, "type": "private"}, "from": {"first_name": "Ryan"}, "text": "/halt now please"}},
         {"update_id": 13, "message": {"message_id": 503, "chat": {"id": 987654321, "type": "private"}, "from": {"first_name": "Ryan"}, "text": "hello"}},
         {"update_id": 14, "message": {"message_id": 504, "chat": {"id": 42, "type": "private"}, "from": {"first_name": "Mallory"}, "text": "/halt"}}],
    ]
    seen: list[tuple] = []
    offsets: list[int] = []
    stop = asyncio.Event()

    async def on_callback(data, by, cb_id, message_id):
        seen.append(("cb", data, by, cb_id, message_id))
        return "Approved ✅"

    async def on_command(cmd, args, by):
        seen.append(("cmd", cmd, args, by))
        if cmd == "/halt":
            stop.set()
        return f"ok {cmd}"

    async def load_offset():
        return 5

    async def save_offset(o):
        offsets.append(o)

    task = asyncio.create_task(bot.poll(stop, on_callback=on_callback, on_command=on_command, load_offset=load_offset, save_offset=save_offset))
    for _ in range(100):
        await clock.run_until(clock.now())
        await asyncio.sleep(0)
        if task.done():
            break
        if clock.pending():
            await clock.advance()
    assert task.done() and task.result() is None
    assert seen == [("cb", "appr:abc:ok", "Ryan", "cb1", 501), ("cmd", "/halt", "now please", "Ryan")]
    polls = [b for m, b in http.calls if m == "getUpdates"]
    assert polls[0]["offset"] == 5 and polls[0]["allowed_updates"] == ["message", "callback_query"] and polls[0]["timeout"] == 20
    assert polls[1]["offset"] == 5                                      # the 409 did not advance the offset
    assert offsets == [15] and bot.errors == 1 and "409" in bot.last_error and bot.updates_seen == 5
    assert [b for m, b in http.calls if m == "answerCallbackQuery"] == [{"callback_query_id": "cb1", "text": "Approved ✅"}]
    replies = [b["text"] for m, b in http.calls if m == "sendMessage"]
    assert replies == ["ok /halt"]


@pytest.mark.asyncio
async def test_unconfigured_bot_refuses_cleanly(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("TT_PROD_CLIENT_ID=x\n", encoding="utf-8")
    bot = TelegramBot(config.load_settings(p, environ={}), TgHttp(), FakeClock(datetime.now(timezone.utc)))
    assert not bot.configured
    with pytest.raises(RuntimeError, match="not configured"):
        await bot.send("x")
    with pytest.raises(TelegramConflict):
        http = TgHttp()
        http.conflict_once = True
        await TelegramBot(config.load_settings(Path(__file__).parent / "nonexistent.env", environ={
            "TELEGRAM_BOT_TOKEN": "123456:telegram-bot-token-value", "TELEGRAM_CHAT_ID": "987654321"}), http, FakeClock(datetime.now(timezone.utc))).get_updates(0)
