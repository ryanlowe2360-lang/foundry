"""The two places the daemon touches the tastytrade SDK's shapes: converting its dxfeed pydantic events into
plain records (field names must match SDK 13.x) and the subscription calls the feed makes. Also the recorder →
replay round trip. No network."""
from __future__ import annotations

import asyncio
import json
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from saa_daemon.clock import et_dt
from saa_daemon.events import CandleEvt, GreeksEvt, ProfileEvt, QuoteEvt, SummaryEvt, TradeEvt, from_sdk
from saa_daemon.feed import DXLinkFeed, FeedPlan, Recorder
from saa_daemon.replay import replay_bars

D = date(2026, 9, 28)


def test_from_sdk_field_names():
    from tastytrade.dxfeed import Candle, Greeks, Profile, Quote, Summary, Trade

    t0 = int(et_dt(D, time(9, 30)).timestamp() * 1000)
    c = Candle(eventSymbol="SPY{=1m,tho=true}", eventTime=0, eventFlags=0, index=1, time=t0, sequence=0, count=40, volume=Decimal("1000"),
               vwap=Decimal("650.1"), bidVolume=None, askVolume=None, impVolatility=None, openInterest=None,
               open=Decimal("650"), high=Decimal("650.6"), low=Decimal("649.9"), close=Decimal("650.5"))
    e = from_sdk(c)
    assert isinstance(e, CandleEvt) and e.symbol == "SPY" and e.time_ms == t0 and e.close == 650.5 and e.volume == 1000.0 and e.count == 40
    q = Quote(eventSymbol=".SPY260928C650", eventTime=0, sequence=0, timeNanoPart=0, bidTime=t0, bidExchangeCode="Q", askTime=t0, askExchangeCode="Q",
              bidPrice=Decimal("1.05"), askPrice=Decimal("1.10"), bidSize=Decimal("12"), askSize=Decimal("30"))
    e = from_sdk(q)
    assert isinstance(e, QuoteEvt) and e.symbol == ".SPY260928C650" and (e.bid, e.ask, e.bid_size, e.ask_size) == (1.05, 1.10, 12.0, 30.0)
    g = Greeks(eventSymbol=".SPY260928C650", eventTime=0, eventFlags=0, index=0, time=t0, sequence=0, price=Decimal("1.07"),
               volatility=Decimal("0.2134"), delta=Decimal("0.51"), gamma=Decimal("0.0812"), theta=Decimal("-0.9"), rho=Decimal("0.01"), vega=Decimal("0.12"))
    e = from_sdk(g)
    assert isinstance(e, GreeksEvt) and e.iv == 0.2134 and e.gamma == 0.0812 and e.delta == 0.51
    s = Summary(eventSymbol=".SPY260928C650", eventTime=0, dayId=20000, dayClosePriceType="REGULAR", prevDayId=19999, prevDayClosePriceType="REGULAR",
                openInterest=15321, dayOpenPrice=Decimal("1.2"), dayHighPrice=Decimal("1.5"), dayLowPrice=Decimal("0.9"), prevDayClosePrice=Decimal("1.1"),
                prevDayVolume=Decimal("5000"))
    e = from_sdk(s)
    assert isinstance(e, SummaryEvt) and e.open_interest == 15321 and e.day_open == 1.2 and e.prev_close == 1.1
    tr = Trade(eventSymbol="SPY", eventTime=0, time=t0, timeNanoPart=0, sequence=0, exchangeCode="Q", dayId=20000, tickDirection="UP",
               extendedTradingHours=False, price=Decimal("650.42"), change=Decimal("0.1"), size=100, dayVolume=Decimal("12345678"), dayTurnover=None)
    e = from_sdk(tr)
    assert isinstance(e, TradeEvt) and e.price == 650.42 and e.day_volume == 12345678.0 and e.size == 100.0
    p = Profile(eventSymbol="IWM", eventTime=0, description="iShares Russell 2000", shortSaleRestriction="INACTIVE", tradingStatus="HALTED",
                haltStartTime=t0, haltEndTime=0, exDividendDayId=0, statusReason="LUDP")
    e = from_sdk(p)
    assert isinstance(e, ProfileEvt) and e.trading_status == "HALTED" and e.halt_start_ms == t0 and e.status_reason == "LUDP"
    # NaN placeholders from dxfeed become None, never a crash
    q2 = Quote(eventSymbol="SPY", eventTime=0, sequence=0, timeNanoPart=0, bidTime=0, bidExchangeCode="", askTime=0, askExchangeCode="",
               bidPrice=Decimal("650"), askPrice=Decimal("650.1"), bidSize="NaN", askSize="NaN")
    e = from_sdk(q2)
    assert isinstance(e, QuoteEvt) and e.bid_size == 0.0 or e.bid_size is None


class _FakeStreamer:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def subscribe(self, cls, symbols, refresh_interval=0.1):
        self.calls.append(("subscribe", cls.__name__, list(symbols), refresh_interval))

    async def subscribe_candle(self, symbols, interval, start_time=None, extended_trading_hours=False, refresh_interval=0.1):
        self.calls.append(("candle", list(symbols), interval, start_time, extended_trading_hours))


@pytest.mark.asyncio
async def test_feed_subscription_plan_is_chunked():
    feed = DXLinkFeed(session=object())
    st = _FakeStreamer()
    opts = {f".SPY260928C{k}" for k in range(600, 920)}   # 320 option symbols → 3 chunks of ≤150
    plan = FeedPlan({"SPY", "QQQ"}, {"SPY", "QQQ"}, opts, et_dt(D, time(9, 30)))
    await feed._subscribe(st, plan)
    kinds = [c[1] for c in st.calls if c[0] == "subscribe"]
    assert kinds[:4] == ["Quote", "Trade", "Summary", "Profile"]                # underlyings first
    candle = [c for c in st.calls if c[0] == "candle"][0]
    assert candle[1] == ["QQQ", "SPY"] and candle[2] == "1m" and candle[3] == et_dt(D, time(9, 30)) and candle[4] is False
    option_calls = [c for c in st.calls if c[0] == "subscribe" and c[2] and c[2][0].startswith(".")]
    assert len(option_calls) == 3 * 4 and max(len(c[2]) for c in option_calls) == 150
    assert {c[3] for c in option_calls if c[1] in ("Quote", "Greeks")} == {1.0} and {c[3] for c in option_calls if c[1] in ("Summary", "Trade")} == {5.0}
    # add() before the socket is up queues the delta; nothing subscribed yet
    await feed.add(FeedPlan({"NVDA"}, {"NVDA"}, {".NVDA260928C180"}))
    assert feed._pending and feed._pending[0].underlyings == {"NVDA"}
    # a delta that is already covered is ignored
    feed._plan = plan
    feed._streamer = st
    n = len(st.calls)
    await feed.add(FeedPlan({"SPY"}, {"SPY"}, set()))
    assert len(st.calls) == n


def test_recorder_replay_roundtrip(tmp_path: Path):
    path = tmp_path / "rec.jsonl"
    rec = Recorder(path, underlying_symbols={"SPY"})
    t0 = et_dt(D, time(9, 30))
    for m in range(5):
        t = t0 + timedelta(minutes=m)
        for frac in (0.3, 1.0):
            rec.maybe(CandleEvt("SPY", int(t.timestamp() * 1000), 650.0, 650.5, 649.8, 650.0 + frac, 1000 * frac, 650.2, int(40 * frac)), int(t.timestamp() * 1000) + 5000)
    rec.maybe(QuoteEvt(".SPY260928C650", 1, 1.0, 1.1), 1)   # option-level → not recorded
    rec.maybe(QuoteEvt("SPY", 1, 650.0, 650.1), 1)
    rec.close()
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 11 and lines[0]["kind"] == "Candle" and lines[-1]["kind"] == "Quote" and "recv_ms" in lines[0]
    out = replay_bars(path)
    assert out["events"] == 11 and out["symbols"]["SPY"]["bars"] == 5 and out["symbols"]["SPY"]["first"] == "09:30"
    assert out["symbols"]["SPY"]["last_close"] == 651.0 and out["symbols"]["SPY"]["gaps"] == []
    assert replay_bars(path) == out    # deterministic
