"""A full trading day through the real daemon loop on a virtual clock.

Everything outside the process is faked (broker sessions, DXLink feed, PostgREST, Cboe, Nasdaq); everything
inside is the real code: Daemon.run_session, supervisor, bar book, chain planning, option book, snapshots,
gamma proxy, SQLite store, mirror flush + queue, heartbeat / EOD texts. The fake feed emits realistic
forming-bar candle updates for 09:30–16:00, option quotes/greeks/OI, one deliberate websocket drop at 11:00
(the second connection replays the day's candles, like dxfeed does), and a halt via Profile.

Asserts the M2 acceptance shape: heartbeat logged, ≥380 one-minute bars per index symbol, a chain snapshot per
active symbol every 5 minutes, zero unhandled exceptions, and the run recorded through the saa_* RPC surface.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from saa_daemon import config
from saa_daemon.clock import ET, FakeClock, Schedule, et_dt
from saa_daemon.daemon import Daemon
from saa_daemon.events import CandleEvt, Evt, GreeksEvt, ProfileEvt, QuoteEvt, SummaryEvt, TradeEvt
from saa_daemon.feed import FeedPlan
from saa_daemon.mirror import SupabaseMirror
from saa_daemon.store import Store
from saa_daemon.telegram import Notifier
from test_daemon_units import FakeHttp


async def _no_broker(_brokerage):
    """M2/M3 simulations: no paper execution (M4 has its own simulation)."""
    return None

D = date(2026, 9, 28)
SPOTS = {"SPY": 650.0, "QQQ": 580.0, "IWM": 240.0, "NVDA": 180.0, "TSLA": 420.0}
OPT_RE = re.compile(r"^\.([A-Z]+)(\d{6})([CP])([\d.]+)$")


# ------------------------------------------------------------------ fakes
@dataclass
class _Strike:
    strike_price: float
    call_streamer_symbol: str
    put_streamer_symbol: str


@dataclass
class _Exp:
    expiration_date: date
    days_to_expiration: int
    strikes: list[_Strike]


@dataclass
class _Nested:
    option_chain_type: str
    expirations: list[_Exp]


@dataclass
class _Info:
    env: str
    ok: bool = True
    error: str | None = None
    account_masked: str | None = None
    account_type: str | None = None
    options_level: str | None = None
    quote_token_ok: bool | None = True


class FakeBrokerage:
    def __init__(self) -> None:
        self.data = object()
        self.broker = object()
        self.data_info = _Info("prod")
        self.broker_info = _Info("sandbox", account_masked="…1234", account_type="Margin", options_level="Basic")
        self.opened = 0
        self.closed = False
        self.chain_calls: list[str] = []

    async def open(self) -> "FakeBrokerage":
        self.opened += 1
        return self

    async def close(self) -> None:
        self.closed = True

    async def spot_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: SPOTS[s] for s in symbols if s in SPOTS}

    async def nested_chain(self, underlying: str) -> _Nested:
        self.chain_calls.append(underlying)
        spot = SPOTS[underlying]
        step = 1.0 if spot > 300 else 0.5 if spot > 100 else 0.25
        exps = []
        for d in (D, D + timedelta(days=1), D + timedelta(days=4)):
            code = f"{d:%y%m%d}"
            ks = [round(spot * 0.9 + i * step, 2) for i in range(int(spot * 0.2 / step) + 1)]
            exps.append(_Exp(d, (d - D).days, [_Strike(k, f".{underlying}{code}C{k:g}", f".{underlying}{code}P{k:g}") for k in ks]))
        return _Nested("Standard", exps)


class FakeFeed:
    """Emits a day of events on the virtual clock. Drops the connection once at 11:00."""

    def __init__(self, clock: FakeClock, *, drop_at: datetime | None):
        self.clock = clock
        self.drop_at = drop_at
        self.dropped = False
        self.plan = FeedPlan()
        self.added: list[FeedPlan] = []
        self.by_kind: dict[str, int] = {}
        self.runs = 0
        self.candle_symbols_seen: set[str] = set()

    def _emit(self, on_event: Callable[[Evt], None], e: Evt) -> None:
        self.by_kind[e.kind] = self.by_kind.get(e.kind, 0) + 1
        on_event(e)

    def _price(self, sym: str, minute: int, frac: float) -> float:
        base = SPOTS[sym]
        return round(base * (1 + 0.004 * math.sin(minute / 23.0) + 0.0006 * math.sin(minute * 1.7 + frac * 6.28)), 2)

    def _option_events(self, on_event, ts_ms: int, full: bool) -> None:
        for sym in sorted(self.plan.options):
            m_ = OPT_RE.match(sym)          # .SPY260928C650 → underlying, right, strike
            assert m_, sym
            und, right, strike = m_.group(1), m_.group(3), float(m_.group(4))
            spot = SPOTS.get(und, 100.0)
            m = abs(strike - spot) / spot
            iv = 0.18 + 0.4 * m + (0.01 if right == "P" else 0.0)
            gamma = max(0.0005, 0.06 * math.exp(-((strike - spot) / (spot * 0.01)) ** 2 / 2))
            intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
            mid = round(intrinsic + 2.5 * math.exp(-m * 60), 2)
            self._emit(on_event, QuoteEvt(sym, ts_ms, max(0.01, mid - 0.03), mid + 0.03, 50, 60))
            self._emit(on_event, GreeksEvt(sym, ts_ms, mid, round(iv, 4), 0.5 if right == "C" else -0.5, round(gamma, 5), -0.1, 0.2))
            if full:
                oi = int(20000 * math.exp(-m * 40)) + (5000 if right == "P" and strike < spot else 0)
                self._emit(on_event, SummaryEvt(sym, ts_ms, oi, None, None, None, None))
                self._emit(on_event, TradeEvt(sym, ts_ms, mid, 1, float(int(oi * 0.3))))

    async def run(self, plan: FeedPlan, on_event: Callable[[Evt], None], stop: asyncio.Event) -> None:
        self.runs += 1
        self.plan = FeedPlan(set(plan.underlyings), set(plan.candles), set(plan.options), plan.candle_start)
        sched = Schedule.for_date(D)
        now = self.clock.now()
        # initial snapshot: underlying quotes/summary/profile + the full option state
        ts_ms = int(now.timestamp() * 1000)
        for u in sorted(self.plan.underlyings):
            px = SPOTS[u]
            self._emit(on_event, QuoteEvt(u, ts_ms, px - 0.01, px + 0.01, 100, 100))
            self._emit(on_event, SummaryEvt(u, ts_ms, None, px, None, None, px * 0.998))
            self._emit(on_event, ProfileEvt(u, ts_ms, "ACTIVE", None, None, None))
        self._option_events(on_event, ts_ms, full=True)
        # dxfeed replays the day's candles from candle_start on (re)connect
        start = self.plan.candle_start or sched.open
        replay_end = min(now, sched.close)
        t = start
        while t < replay_end:
            minute = int((t - sched.open).total_seconds() // 60)
            for s in sorted(self.plan.candles):
                o, c = self._price(s, minute, 0.0), self._price(s, minute, 0.9)
                self._emit(on_event, CandleEvt(s, int(t.timestamp() * 1000), o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1000.0, (o + c) / 2, 40))
            t += timedelta(minutes=1)
        # live: three forming-bar updates per minute, quotes each update, option refresh every 5 minutes
        t = max(start, now.replace(second=0, microsecond=0))
        while t < sched.close and not stop.is_set():
            minute = int((t - sched.open).total_seconds() // 60)
            for offs, frac in ((5, 0.1), (30, 0.5), (55, 0.9)):
                wake = t + timedelta(seconds=offs)
                if wake <= self.clock.now():
                    continue
                await self.clock.sleep_until(wake)
                if stop.is_set():
                    return
                if self.drop_at and not self.dropped and wake >= self.drop_at:
                    self.dropped = True
                    raise ConnectionResetError("simulated websocket drop")
                ts_ms = int(wake.timestamp() * 1000)
                for s in sorted(self.plan.candles):
                    o, c = self._price(s, minute, 0.0), self._price(s, minute, frac)
                    self._emit(on_event, CandleEvt(s, int(t.timestamp() * 1000), o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1000.0 * frac, (o + c) / 2, int(40 * frac)))
                    self.candle_symbols_seen.add(s)
                for u in sorted(self.plan.underlyings):
                    px = self._price(u, minute, frac)
                    self._emit(on_event, QuoteEvt(u, ts_ms, px - 0.01, px + 0.01, 100, 100))
                    self._emit(on_event, TradeEvt(u, ts_ms, px, 100, 1e6 * (minute + 1)))
                if offs == 55 and minute % 5 == 4:
                    self._option_events(on_event, ts_ms, full=(minute % 30 == 29))
                if offs == 30 and minute == 15 and "IWM" in self.plan.underlyings:   # a halt via Profile at 09:45
                    self._emit(on_event, ProfileEvt("IWM", ts_ms, "HALTED", ts_ms, None, "LUDP"))
                if offs == 55 and minute == 16 and "IWM" in self.plan.underlyings:
                    self._emit(on_event, ProfileEvt("IWM", ts_ms, "ACTIVE", None, None, None))
            t += timedelta(minutes=1)
        await stop.wait()

    async def add(self, delta: FeedPlan) -> None:
        self.added.append(delta)
        self.plan.merge(delta)


# --------------------------------------------------------------- the test
@pytest.mark.asyncio
async def test_full_session_simulation(env_file: Path, tmp_path: Path, fixtures: Path):
    settings = config.load_settings(env_file, environ={"SAA_MAX_SINGLE_NAMES": "2"}, state_dir=tmp_path / "state")
    sched = Schedule.for_date(D)
    clock = FakeClock(et_dt(D, time(9, 15)))
    store = Store(tmp_path / "state" / "saa.sqlite")
    http = FakeHttp(fixtures)
    mirror = SupabaseMirror(settings, store, http)
    notifier = Notifier(settings, mirror, http)
    brokerage = FakeBrokerage()
    feed = FakeFeed(clock, drop_at=et_dt(D, time(11, 0)))
    daemon = Daemon(settings, clock=clock, store=store, http=http, brokerage=brokerage, feed_factory=lambda _b: feed, broker_factory=_no_broker,
                    mirror=mirror, notifier=notifier, mode="session", trade_date=D, host="testmac")

    task = asyncio.create_task(daemon.run_session())
    deadline = sched.shutdown + timedelta(minutes=10)
    for _ in range(200):
        await clock.run_until(deadline)
        if task.done():
            break
        await asyncio.sleep(0)
    assert task.done(), "daemon did not finish on the virtual clock"
    res = task.result()

    # --- zero unhandled exceptions; the one deliberate drop was caught and the feed reconnected
    assert res.unhandled == 0 and res.ok
    assert res.errors == {"feed": 1}, res.errors
    assert feed.runs == 2 and feed.dropped and daemon.feed_reconnects == 1

    # --- universe: settings index symbols (DB setting agrees) + active single names capped at 2
    assert daemon.index_symbols == ["SPY", "QQQ", "IWM"] and daemon.single_names == ["NVDA", "TSLA"]
    assert set(daemon.plans) == {"SPY", "QQQ", "IWM", "NVDA", "TSLA"}
    assert len(daemon.plans["SPY"].expirations) == 2 and len(daemon.plans["NVDA"].expirations) == 1
    assert daemon.plans["SPY"].expirations[0].dte == 0 and daemon.plans["SPY"].n_strikes == 78    # 39 strikes × 2 expirations

    # --- ≥380 complete 1-minute bars per index symbol, no gaps, despite the reconnect replay
    stats = res.stats
    for sym in ("SPY", "QQQ", "IWM"):
        b = stats["bars"][sym]
        assert b["expected"] == 390 and b["complete"] >= 380 and b["missing"] == 0, (sym, b)
        assert store.bar_stats(sym, sched.open, sched.close)["complete"] == 390
    row = store.db.execute("select * from bars_1m where symbol='SPY' and bar_time=?", ("2026-09-28T13:30:00Z",)).fetchone()
    # the 11:00 reconnect replayed the day's history: the final candle (volume 1000 / 40 trades) replaced the forming values
    assert row["complete"] == 1 and row["volume"] == pytest.approx(1000.0) and row["trade_count"] == 40
    late = store.db.execute("select * from bars_1m where symbol='SPY' and bar_time=?", ("2026-09-28T19:59:00Z",)).fetchone()
    assert late["complete"] == 1 and late["volume"] == pytest.approx(900.0) and late["trade_count"] == 36   # 15:59 bar: last forming update won

    # --- one chain snapshot per active symbol every 5 minutes (80 ticks: 09:25 baseline + 09:30…16:00)
    assert stats["snapshots_expected"] == 80
    assert stats["snapshots"] == {s: 80 for s in ("SPY", "QQQ", "IWM", "NVDA", "TSLA")}, stats["snapshots"]
    snap = store.latest_snapshot("SPY")
    assert snap["spot"] and len(snap["expirations"]) == 2 and len(snap["expirations"][0]["strikes"]) == 39
    assert snap["summary"]["coverage"] == {"quotes": 1.0, "greeks": 1.0, "oi": 1.0} and snap["summary"]["atm_iv"] is not None
    assert snap["gamma"]["regime"] in ("positive", "negative") and snap["gamma"]["coverage"] == 1.0
    assert stats["gamma_open"]["regime"] in ("positive", "negative") and stats["gamma_close"]["regime"] in ("positive", "negative")

    # --- VIX polled (fixture values), halts from RSS + the Profile halt
    assert stats["vix_open"]["vix"] == 14.87 and stats["vix_open"]["vix3m"] == 17.93 and stats["vix_open"]["shape"] == "contango"
    assert len(store.vix_range(sched.prep, sched.shutdown)) >= 78
    halts = {(h["symbol"], h["source"]) for h in stats["halts"]}
    assert ("ABCD", "nasdaq_rss") in halts and ("EFGH", "nasdaq_rss") in halts and ("IWM", "dxlink_profile") in halts

    # --- feed lag monitor: the fake stamps trades with the virtual clock → real-time
    assert stats["feed"]["mode"] == "realtime" and stats["feed"]["lag_s"] is not None and stats["feed"]["lag_s"] < 30 and stats["feed"]["n"] > 100

    # --- Telegram: heartbeat at 09:25 and the EOD report at 16:20, both through saa.outbox
    enq = [b for fn, b in http.calls if fn == "saa_enqueue"]
    assert len(enq) == 2 and enq[0]["p_kind"] == "system" and enq[0]["p_text"].startswith("SAA daemon ▸ Mon 2026-09-28 · v0.3.0 · testmac")
    assert "sandbox ok (acct …1234, Margin, options Basic) | Data: prod DXLink ok" in enq[0]["p_text"]
    assert "Universe: SPY QQQ IWM + 2 names (NVDA TSLA)" in enq[0]["p_text"] and "Econ today: Dallas Fed 10:30" in enq[0]["p_text"]
    assert "VIX 14.9 · 1D 12.5 · 9D 12.8 · 3M 17.9 (contango)" in enq[0]["p_text"]
    eod = enq[1]["p_text"]
    assert eod.startswith("SAA daemon EOD ▸ 2026-09-28 · run 09:15–16:20 ET · 0 unhandled exceptions")
    assert "IWM 390/390" in eod and "QQQ 390/390" in eod and "SPY 390/390" in eod
    assert "80 snapshots × 5 underlyings (expected 80)" in eod and "1 reconnects · lag 0s real-time · errors caught: feed 1" in eod
    assert [m["path"] for m in res.messages] == ["outbox", "outbox"]

    # --- mirrored into saa.*: every bar + snapshot pushed, run row patched to done, run_log has start/heartbeat/session
    calls_by_fn: dict[str, list[dict]] = {}
    for fn, body in http.calls:
        calls_by_fn.setdefault(fn, []).append(body)
    pushed_bars = {(r["symbol"], r["bar_time"]) for b in calls_by_fn["saa_bars_upsert"] for r in b["p_rows"]}
    assert len(pushed_bars) == 390 * 5 and all(r["complete"] for r in calls_by_fn["saa_bars_upsert"][-1]["p_rows"])
    assert len(calls_by_fn["saa_chain_snapshot"]) == 80 * 5
    assert len(calls_by_fn["saa_snapshot"]) >= 78 and all(b["p_kind"] == "vix_term" for b in calls_by_fn["saa_snapshot"])
    jobs = [b["p_job"] for b in calls_by_fn["saa_log_run"]]
    assert jobs[0] == "daemon:start" and "daemon:heartbeat" in jobs and jobs[-1] == "daemon:session"
    session_log = calls_by_fn["saa_log_run"][-1]
    assert session_log["p_ok"] is True and session_log["p_detail"]["unhandled"] == 0 and session_log["p_detail"]["bars"]["SPY"]["complete"] == 390
    runs = calls_by_fn["saa_daemon_run"]
    assert runs[0]["p_patch"]["mode"] == "session" and runs[-1]["p_patch"]["status"] == "done" and runs[-1]["p_patch"]["stats"]["snapshots"]["SPY"] == 80
    assert len(runs) >= 390  # the 60-second machine heartbeat kept firing all day
    assert store.queue_size() == 0 and store.dirty_bars() == [] and store.dirty_snapshots() == []
    assert store.get_run(res.run_id)["status"] == "done"
    assert brokerage.closed

    # --- no secret ever reached a log record or an RPC body
    blob = str(http.calls)
    for s in settings.secret_values():
        assert s not in blob
