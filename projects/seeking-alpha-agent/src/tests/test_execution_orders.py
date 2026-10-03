"""M4 order path against the FakeBroker: symbol conversion, tick rounding, the limit-at-mid retry ladder, partial fills,
rejections, cancellation, and fill logging — all on a virtual clock, no network, no SDK."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path

import pytest

from saa_daemon.clock import FakeClock, et_dt
from saa_daemon.execution.broker import BrokerError, FakeBroker
from saa_daemon.execution.orders import LadderPolicy, OrderManager, Ticket, ladder_prices
from saa_daemon.execution.symbols import occ_to_streamer, parse_streamer, round_to_tick, streamer_to_occ, tick_size
from saa_daemon.store import Store

D = date(2026, 9, 28)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return et_dt(D, time(hh, mm, ss))


# ------------------------------------------------------------------------------------------------------------ symbols
def test_streamer_to_occ_and_back():
    assert streamer_to_occ(".SPY260928C650") == "SPY   260928C00650000"
    assert streamer_to_occ(".SPY260928C650.5") == "SPY   260928C00650500"
    assert streamer_to_occ(".NVDA261016P177.5") == "NVDA  261016P00177500"
    assert streamer_to_occ(".TSLA260928P420") == "TSLA  260928P00420000"
    for s in (".SPY260928C650", ".SPY260928C650.5", ".NVDA261016P177.5", ".GOOGL260928C150"):
        assert occ_to_streamer(streamer_to_occ(s)) == s
    with pytest.raises(ValueError):
        streamer_to_occ("SPY")


def test_parse_streamer():
    und, exp, right, strike = parse_streamer(".SPY260928C650.5")
    assert (und, exp, right, strike) == ("SPY", date(2026, 9, 28), "C", 650.5)


def test_tick_rounding_toward_the_far_side():
    assert tick_size(1.16) == 0.01 and tick_size(2.99) == 0.01 and tick_size(3.00) == 0.05 and tick_size(12.5) == 0.05
    # a buyer rounds up (toward the ask), a seller rounds down (toward the bid)
    assert round_to_tick(1.155, "buy") == 1.16 and round_to_tick(1.155, "sell") == 1.15
    assert round_to_tick(3.12, "buy") == 3.15 and round_to_tick(3.12, "sell") == 3.10
    assert round_to_tick(1.16, "buy") == 1.16 and round_to_tick(0.004, "buy") == 0.01 and round_to_tick(0.004, "sell") == 0.01


def test_ladder_prices_walk_from_mid_to_the_far_side():
    # buy: mid 1.15 (bid 1.10 / ask 1.20) → 1.15, 1.17, 1.18, 1.20 (far side last), never above the ask
    assert ladder_prices("buy", 1.10, 1.20, steps=3) == [1.15, 1.17, 1.18, 1.20]
    assert ladder_prices("sell", 1.10, 1.20, steps=3) == [1.15, 1.13, 1.12, 1.10]
    assert ladder_prices("buy", 1.10, 1.20, steps=0) == [1.15]
    # a one-tick spread: mid rounds toward the far side and the ladder collapses onto it
    assert ladder_prices("buy", 1.15, 1.16, steps=3) == [1.16]
    assert ladder_prices("sell", 1.15, 1.16, steps=3) == [1.15]


def test_tick_table_by_option_class():
    from saa_daemon.execution.symbols import PENNY_ALL, PENNY_PROGRAM, nearest_tick, root_of, set_ticks, set_ticks_from_chain, ticks_for
    # SPY / QQQ / IWM / XSP trade in pennies at every price; other penny-program classes step to $0.05 at $3; unknown = penny program
    assert tick_size(4.76, "SPY   261009C00770000") == 0.01 and tick_size(4.76, ".QQQ261009C480") == 0.01 and tick_size(9.9, "IWM") == 0.01
    assert tick_size(4.76, ".NVDA261016C177.5") == 0.05 and tick_size(2.99, ".NVDA261016C177.5") == 0.01 and tick_size(4.76) == 0.05
    assert ticks_for("SPY") == PENNY_ALL and ticks_for(".ZZZ261009C10") == PENNY_PROGRAM and ticks_for(None) == PENNY_PROGRAM
    assert root_of(".SPY261009C770") == "SPY" and root_of("SPXW  261009C06500000") == "SPXW" and root_of("nvda") == "NVDA" and root_of("") is None
    # the broker's chain carries the authoritative rules — a non-penny class steps $0.05 / $0.10
    class _TS:
        def __init__(self, value, threshold=None):
            self.value, self.threshold = value, threshold
    class _Chain:
        root_symbol, underlying_symbol = "ZZT", "ZZT"
        tick_sizes = [_TS("0.05", "3"), _TS("0.1")]
    assert set_ticks_from_chain(_Chain()) == ((0.05, 3.0), (0.1, None))
    assert tick_size(1.23, ".ZZT261016C10") == 0.05 and tick_size(3.0, "ZZT") == 0.1 and round_to_tick(1.23, "buy", "ZZT") == 1.25
    assert round_to_tick(3.21, "sell", "ZZT") == 3.2 and nearest_tick(3.26, "ZZT") == 3.3
    class _Spy:
        root_symbol, underlying_symbol = "SPY", "SPY"
        tick_sizes = [_TS("0.01")]
    assert set_ticks_from_chain(_Spy()) == PENNY_ALL                       # re-registering SPY from its chain keeps pennies everywhere
    class _Empty:
        root_symbol, tick_sizes = "QQQ", []
    assert set_ticks_from_chain(_Empty()) is None and ticks_for("QQQ") == PENNY_ALL
    assert set_ticks("ZZU", [(0.05, 3.0)]) == ((0.05, 3.0), (0.05, None))   # a rule set without an unbounded tail keeps its last tick
    # SPY above $3 ladders in pennies: bid 4.75 / ask 4.77 → mid 4.76 first, then the ask; an unknown penny-program class
    # at the same quote cannot sit on 4.76 (grid 0.05) and starts at the ask
    assert ladder_prices("buy", 4.75, 4.77, steps=3, symbol=".SPY261009C770") == [4.76, 4.77]
    assert ladder_prices("sell", 4.75, 4.77, steps=3, symbol="SPY   261009C00770000") == [4.76, 4.75]
    assert ladder_prices("buy", 4.75, 4.77, steps=3, symbol=".NVDA261016C177.5") == [4.77]
    assert ladder_prices("buy", 4.75, 4.77, steps=3) == [4.77]


@pytest.mark.asyncio
async def test_ladder_uses_the_class_grid_and_carries_broker_warnings(tmp_path: Path):
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="never")
    b.now_fn = clock.now
    b.warnings = ["tif.next_valid_session: Your order will begin working during next valid session."]
    occ = "SPY   261009C00770000"
    b.quotes[occ] = (4.75, 4.77)
    om = OrderManager(b, Store(tmp_path / "saa.sqlite"), clock, LadderPolicy(), run_id="r", trade_date=D)
    task = asyncio.create_task(om.work(Ticket.new("k", ".SPY261009C770", "buy", 1, 4.75, 4.77, clock.now())))
    await _drive(clock, task, at(10, 1))
    t = task.result()
    assert t.status == "unfilled" and t.limit_prices == [4.76, 4.77] and t.warnings == b.warnings and t.row()["warnings"] == b.warnings


# --------------------------------------------------------------------------------------------------------- fake broker
@pytest.mark.asyncio
async def test_fake_broker_fills_marketable_limits_and_tracks_positions():
    b = FakeBroker()
    b.quotes["SPY   260928C00650000"] = (1.10, 1.20)
    o = await b.place("SPY   260928C00650000", "buy_to_open", 2, 1.15)
    assert o.status == "live" and o.filled_quantity == 0            # 1.15 < ask 1.20: resting
    o2 = await b.replace(o.order_id, 1.20)
    assert o2.order_id != o.order_id and o2.status == "filled" and o2.filled_quantity == 2 and o2.avg_fill_price == 1.20
    assert (await b.get_order(o.order_id)).status == "replaced"
    pos = await b.positions()
    assert len(pos) == 1 and pos[0].symbol == "SPY   260928C00650000" and pos[0].quantity == 2 and pos[0].average_open_price == 1.20
    s = await b.place("SPY   260928C00650000", "sell_to_close", 2, 1.05)
    assert s.status == "filled" and s.avg_fill_price == 1.10        # sell limit below the bid fills at the bid
    assert await b.positions() == []
    assert [c[0] for c in b.calls] == ["place", "replace", "get_order", "positions", "place", "positions"]


@pytest.mark.asyncio
async def test_fake_broker_modes():
    b = FakeBroker(mode="at_limit")                                   # every limit fills at its limit price, whatever the quote
    b.quotes["X"] = (1.00, 1.10)
    o = await b.place("X", "buy_to_open", 1, 1.03)
    assert o.status == "filled" and o.avg_fill_price == 1.03
    b = FakeBroker(mode="reject", reject_reason="options level does not permit this order")
    b.quotes["X"] = (1.00, 1.10)
    o = await b.place("X", "buy_to_open", 1, 1.10)
    assert o.status == "rejected" and "options level" in (o.reject_reason or "")
    b = FakeBroker(mode="never")
    b.quotes["X"] = (1.00, 1.10)
    o = await b.place("X", "buy_to_open", 1, 1.10)
    assert o.status == "live"
    c = await b.cancel(o.order_id)
    assert c.status == "cancelled"
    b = FakeBroker(fail_next=["place"])
    with pytest.raises(BrokerError):
        await b.place("X", "buy_to_open", 1, 1.10)


# -------------------------------------------------------------------------------------------------------- the ladder
def _mgr(tmp_path: Path, clock: FakeClock, broker: FakeBroker, policy: LadderPolicy | None = None) -> OrderManager:
    store = Store(tmp_path / "saa.sqlite")
    return OrderManager(broker, store, clock, policy or LadderPolicy(step_seconds=5.0, steps=3, fill_wait_seconds=5.0, poll_seconds=1.0),
                        run_id="test-run", trade_date=D)


async def _drive(clock: FakeClock, task: asyncio.Task, until: datetime) -> None:
    for _ in range(500):
        await clock.run_until(until)
        if task.done():
            return
        await asyncio.sleep(0)
    assert task.done(), "ladder did not finish on the virtual clock"


@pytest.mark.asyncio
async def test_ladder_fills_at_mid_immediately_when_marketable(tmp_path: Path):
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker(mode="at_limit")
    b.quotes["SPY   260928C00654000"] = (1.12, 1.16)
    m = _mgr(tmp_path, clock, b)
    t = await m.work(Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.12, 1.16, clock.now()))
    assert t.status == "filled" and t.filled_quantity == 1 and t.avg_fill_price == 1.14 and t.limit_prices == [1.14]
    assert len(t.broker_order_ids) == 1 and t.done_at == clock.now() and t.fills and t.fills[0]["price"] == 1.14
    rows = m.store.paper_orders(D.isoformat())
    assert len(rows) == 1 and rows[0]["status"] == "filled" and rows[0]["payload"]["avg_fill_price"] == 1.14


@pytest.mark.asyncio
async def test_ladder_steps_toward_the_ask_and_fills_on_the_way(tmp_path: Path):
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker()                                     # marketable-only fills: the buy must reach the ask
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    task = asyncio.create_task(m.work(Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.10, 1.20, clock.now())))
    await _drive(clock, task, at(9, 38, 30))
    t = task.result()
    assert t.status == "filled" and t.limit_prices == [1.15, 1.17, 1.18, 1.20] and t.avg_fill_price == 1.20
    assert len(t.broker_order_ids) == 4 and (t.done_at - t.created_at).total_seconds() == pytest.approx(15.0)
    assert [c[0] for c in b.calls].count("replace") == 3


@pytest.mark.asyncio
async def test_ladder_re_reads_the_quote_at_every_step(tmp_path: Path):
    """The market moves while the ladder works: step prices follow the live quote, never the stale one."""
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker()
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    quotes = iter([(1.10, 1.20), (1.30, 1.40), (1.30, 1.40), (1.30, 1.40)])

    def quote():
        return next(quotes)

    task = asyncio.create_task(m.work(Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.10, 1.20, clock.now()), quote_fn=quote))
    await clock.run_until(at(9, 37, 6))
    b.quotes["SPY   260928C00654000"] = (1.30, 1.40)
    await _drive(clock, task, at(9, 38, 30))
    t = task.result()
    assert t.status == "filled" and t.limit_prices[0] == 1.15 and t.limit_prices[1] >= 1.30 and t.avg_fill_price == 1.40


@pytest.mark.asyncio
async def test_ladder_gives_up_and_cancels_when_never_filled(tmp_path: Path):
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker(mode="never")
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    task = asyncio.create_task(m.work(Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.10, 1.20, clock.now())))
    await _drive(clock, task, at(9, 40, 0))
    t = task.result()
    assert t.status == "unfilled" and t.filled_quantity == 0 and t.limit_prices == [1.15, 1.17, 1.18, 1.20]
    assert (await b.get_order(t.broker_order_ids[-1])).status == "cancelled"
    assert (t.done_at - t.created_at).total_seconds() == pytest.approx(20.0)      # 3 steps × 5 s + 5 s final wait
    assert "unfilled after the ladder" in t.reason


@pytest.mark.asyncio
async def test_ladder_partial_fill_is_reported(tmp_path: Path):
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker(mode="partial")                       # fills half at the limit, the rest never
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    task = asyncio.create_task(m.work(Ticket.new("k1", ".SPY260928C654", "buy", 2, 1.10, 1.20, clock.now())))
    await _drive(clock, task, at(9, 40, 0))
    t = task.result()
    assert t.status == "partial" and t.filled_quantity == 1 and t.avg_fill_price == 1.15
    assert len(t.broker_order_ids) == 1 and "partial" in t.reason  # a partially filled order is never replaced, only cancelled


@pytest.mark.asyncio
async def test_rejected_order_is_logged_with_the_reason(tmp_path: Path):
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker(mode="reject", reject_reason="Account does not have permission for this order")
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    t = await m.work(Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.10, 1.20, clock.now()))
    assert t.status == "rejected" and "permission" in t.reason and t.filled_quantity == 0
    rows = m.store.paper_orders(D.isoformat())
    assert rows[0]["status"] == "rejected" and "permission" in rows[0]["payload"]["reason"]


@pytest.mark.asyncio
async def test_broker_error_during_placement_is_an_error_status(tmp_path: Path):
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker(fail_next=["place"])
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    t = await m.work(Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.10, 1.20, clock.now()))
    assert t.status == "error" and "BrokerError" in t.reason and t.broker_order_ids == []


@pytest.mark.asyncio
async def test_cancel_working_ticket_from_outside(tmp_path: Path):
    """The executor cancels a working entry when the engine closes the position before the ladder finished."""
    clock = FakeClock(at(9, 37, 2))
    b = FakeBroker(mode="never")
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    ticket = Ticket.new("k1", ".SPY260928C654", "buy", 1, 1.10, 1.20, clock.now())
    task = asyncio.create_task(m.work(ticket))
    await clock.run_until(at(9, 37, 4))
    await m.cancel(ticket, "engine closed the position")
    await _drive(clock, task, at(9, 38, 0))
    t = task.result()
    assert t.status == "cancelled" and t.reason == "engine closed the position" and t.filled_quantity == 0
    assert (await b.get_order(t.broker_order_ids[-1])).status == "cancelled"


@pytest.mark.asyncio
async def test_flatten_ladder_reaches_market_within_ten_seconds(tmp_path: Path):
    """The kill-switch exit: bid → bid − a step at 3 s → market at 6 s; a stubborn book is flat well inside 10 s."""
    clock = FakeClock(at(10, 0, 0))
    b = FakeBroker(mode="never", market_fills=True)      # limits never fill, market orders always do
    b.quotes["SPY   260928C00654000"] = (1.10, 1.20)
    m = _mgr(tmp_path, clock, b)
    task = asyncio.create_task(m.flatten(Ticket.new("k1", ".SPY260928C654", "sell", 1, 1.10, 1.20, clock.now(), kind="flatten")))
    await _drive(clock, task, at(10, 0, 30))
    t = task.result()
    assert t.status == "filled" and t.avg_fill_price == 1.10 and (t.done_at - t.created_at).total_seconds() <= 10
    assert t.limit_prices[0] == 1.10 and t.limit_prices[1] < 1.10 and t.limit_prices[-1] is None       # None = market
    assert [c[0] for c in b.calls if c[0] in ("place", "replace")] == ["place", "replace", "replace"]


# ------------------------------------------------------------------------------------ the tastytrade sandbox's fill rule
SBX = "SPY   261009C00770000"


@pytest.mark.asyncio
async def test_fake_broker_sandbox_mode_follows_the_documented_fill_rule():
    """developer.tastytrade.com/docs/sandbox: a limit order priced under $3 fills immediately, one at $3 or above goes
    Live and never fills, a market order always fills — at $1. There is no market behind it (the sandbox serves no quotes)."""
    from saa_daemon.execution.broker import SANDBOX_LIMIT_FILLS_BELOW, SANDBOX_MARKET_FILL_PRICE
    assert SANDBOX_LIMIT_FILLS_BELOW == 3.00 and SANDBOX_MARKET_FILL_PRICE == 1.00
    b = FakeBroker(mode="sandbox")                                    # no quotes set on purpose
    o = await b.place(SBX, "buy_to_open", 1, 2.99)
    assert o.status == "filled" and o.avg_fill_price == 2.99 and o.filled_quantity == 1
    for px in (3.00, 4.76):
        o = await b.place(SBX, "buy_to_open", 1, px)
        assert o.status == "live" and (await b.get_order(o.order_id)).status == "live" and o.filled_quantity == 0
        assert (await b.cancel(o.order_id)).status == "cancelled"
    s = await b.place(SBX, "sell_to_close", 1, 3.40)                  # the same rule on the way out: a winner above $3 never sells at a limit
    assert s.status == "live"
    await b.cancel(s.order_id)
    m = await b.place(SBX, "sell_to_close", 1, None)
    assert m.status == "filled" and m.avg_fill_price == 1.00 and await b.positions() == []


@pytest.mark.asyncio
async def test_ladder_in_the_sandbox_only_fills_a_contract_priced_under_three_dollars(tmp_path: Path):
    """Ryan's Saturday pick (.SPY261009C770 at 4.75 / 4.77) can never fill in the sandbox, on any day; a 1.20 / 1.22 strike fills on rung 0."""
    clock = FakeClock(at(10, 0, 0))
    b = FakeBroker(mode="sandbox")
    b.now_fn = clock.now
    m = _mgr(tmp_path, clock, b)
    task = asyncio.create_task(m.work(Ticket.new("k-rich", ".SPY261009C770", "buy", 1, 4.75, 4.77, clock.now())))
    await _drive(clock, task, at(10, 1, 0))
    t = task.result()
    assert t.status == "unfilled" and t.filled_quantity == 0 and t.limit_prices == [4.76, 4.77] and await b.live_orders() == []
    t2 = await m.work(Ticket.new("k-cheap", ".SPY261009C777", "buy", 1, 1.20, 1.22, clock.now()))
    assert t2.status == "filled" and t2.avg_fill_price == 1.21 and t2.limit_prices == [1.21] and t2.reason == "filled on rung 0 @ 1.21"


@pytest.mark.asyncio
async def test_flatten_in_the_sandbox_falls_through_to_the_dollar_market_fill_above_three_dollars(tmp_path: Path):
    """The kill switch on a sandbox position marked at $3 or more: neither limit rung can fill, the market order at 6 s
    does (at the sandbox's $1) — still flat inside the 10-second budget. Under $3 the first order fills."""
    clock = FakeClock(at(10, 0, 0))
    b = FakeBroker(mode="sandbox")
    b.now_fn = clock.now
    m = _mgr(tmp_path, clock, b)
    task = asyncio.create_task(m.flatten(Ticket.new("k-rich", ".SPY261009C770", "sell", 1, 3.40, 3.44, clock.now(), kind="flatten")))
    await _drive(clock, task, at(10, 0, 30))
    t = task.result()
    assert t.status == "filled" and t.avg_fill_price == 1.00 and t.limit_prices == [3.40, 3.23, None]
    assert (t.done_at - t.created_at).total_seconds() <= 10
    t2 = await m.flatten(Ticket.new("k-cheap", ".SPY261009C777", "sell", 1, 1.20, 1.22, clock.now(), kind="flatten"))
    assert t2.status == "filled" and t2.avg_fill_price == 1.20 and t2.limit_prices == [1.20] and t2.done_at == t2.created_at
