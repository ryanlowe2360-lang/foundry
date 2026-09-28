"""Unit tests for the M2 daemon's pure pieces: config/redaction, clock/schedule, bar book, chain planning,
snapshot building, dealer-gamma proxy, Cboe/Nasdaq parsers, SQLite store, mirror payloads + queue, reports.
Run: `python3 -m pytest src/tests -q` from projects/seeking-alpha-agent (no network needed)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from saa_daemon import bars as bars_mod
from saa_daemon import config, reports
from saa_daemon.chains import OptionBook, RawExpiration, build_snapshot, plan_chain
from saa_daemon.clock import ET, FakeClock, Schedule, align_up, et_dt, is_trading_day, next_trading_day
from saa_daemon.events import CandleEvt, GreeksEvt, ProfileEvt, QuoteEvt, SummaryEvt, TradeEvt, candle_ticker, from_record, to_record
from saa_daemon.gamma import GexInput, dealer_gamma_proxy, dollar_gamma
from saa_daemon.halts import halt_from_profile, parse_halts_rss
from saa_daemon.log import RedactFilter
from saa_daemon.mirror import MirrorError, SupabaseMirror
from saa_daemon.store import BarRow, Store
from saa_daemon.telegram import Notifier
from saa_daemon.vix import parse_cboe_quote, term_shape

UTC = timezone.utc
D = date(2026, 9, 28)  # Monday


# ----------------------------------------------------------------------------- config
def test_env_parse_and_redaction(env_file: Path):
    s = config.load_settings(env_file, environ={}, state_dir=env_file.parent / "state")
    assert s.tt_prod_client_id == "prod-client-id"
    assert s.tt_sandbox_client_secret.value == "sbx-secret-value-0123456789"     # quotes stripped
    assert s.tt_sandbox_refresh_token.value == "sbx-refresh-token-abcdefghij"
    assert s.supabase_url == "https://example.supabase.co"                        # trailing slash removed
    assert s.mirror_configured and s.telegram_fallback_configured
    assert s.missing_tt_keys("prod") == [] and s.missing_tt_keys("sandbox") == []
    # nothing secret in any printable form
    dump = repr(s) + str(s.redacted()) + str(s.tt_prod_client_secret) + repr(s.supabase_key)
    for secret in s.secret_values():
        assert secret not in dump
    assert "<secret len=" in dump
    assert config.readiness(s) == []


def test_env_overrides_and_guards(env_file: Path):
    s = config.load_settings(env_file, environ={"SAA_INDEX_SYMBOLS": "spy, qqq ,xsp", "SAA_MIRROR": "false", "SAA_MAX_SINGLE_NAMES": "7"}, state_dir=env_file.parent)
    assert s.index_symbols == ("SPY", "QQQ", "XSP") and s.index_symbols_explicit
    assert s.mirror_enabled is False and s.max_single_names == 7
    with pytest.raises(config.ConfigError, match="sandbox-only"):
        config.load_settings(env_file, environ={"SAA_BROKER_ENV": "prod"}, state_dir=env_file.parent)
    with pytest.raises(config.ConfigError):
        config.load_settings(env_file, environ={"SAA_DATA_ENV": "paper"}, state_dir=env_file.parent)


def test_missing_keys_named_not_valued(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("TT_PROD_CLIENT_ID=x\n", encoding="utf-8")
    s = config.load_settings(p, environ={}, state_dir=tmp_path)
    probs = config.readiness(s)
    assert any("TT_PROD_CLIENT_SECRET" in x and "TT_PROD_REFRESH_TOKEN" in x for x in probs)
    assert any("TT_SANDBOX_CLIENT_ID" in x for x in probs)
    assert not s.mirror_configured


def test_log_redaction_filter():
    f = RedactFilter(["prod-secret-value-0123456789"])
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "token is %s here", ("prod-secret-value-0123456789",), None)
    assert f.filter(rec) and rec.getMessage() == "token is <redacted> here"


# ------------------------------------------------------------------------------ clock
def test_trading_calendar_and_schedule():
    assert is_trading_day(D) and not is_trading_day(date(2026, 9, 27)) and not is_trading_day(date(2026, 11, 26))
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    s = Schedule.for_date(D)
    assert s.open.astimezone(ET).time() == time(9, 30) and s.close.astimezone(ET).time() == time(16, 0)
    assert s.heartbeat.astimezone(ET).time() == time(9, 25) and s.report.astimezone(ET).time() == time(16, 20)
    assert s.session_minutes == 390 and not s.early_close
    ticks = s.snapshot_ticks(5)
    assert len(ticks) == 80 and ticks[0] == s.heartbeat and ticks[1] == s.open and ticks[-1] == s.close
    e = Schedule.for_date(date(2026, 11, 27))
    assert e.early_close and e.close.astimezone(ET).time() == time(13, 0) and e.report.astimezone(ET).time() == time(13, 20)
    # DST: 9:30 ET is 13:30Z in September and 14:30Z in December
    assert et_dt(D, time(9, 30)).hour == 13 and et_dt(date(2026, 12, 1), time(9, 30)).hour == 14


def test_align_up():
    t = et_dt(D, time(9, 31)) + timedelta(seconds=7)
    assert align_up(t, 5).astimezone(ET).time() == time(9, 35)
    exact = et_dt(D, time(9, 35))
    assert align_up(exact, 5) == exact


@pytest.mark.asyncio
async def test_fake_clock_orders_wakeups():
    start = et_dt(D, time(9, 0))
    clk = FakeClock(start)
    order: list[str] = []

    async def sleeper(name: str, secs: float) -> None:
        await clk.sleep(secs)
        order.append(name)

    tasks = [asyncio.create_task(sleeper("b", 20)), asyncio.create_task(sleeper("a", 10)), asyncio.create_task(sleeper("c", 30))]
    await clk.run_until(start + timedelta(seconds=25))
    assert order == ["a", "b"] and clk.now() == start + timedelta(seconds=25)
    await clk.run_until(start + timedelta(seconds=60))
    assert order == ["a", "b", "c"]
    await asyncio.gather(*tasks)


# ------------------------------------------------------------------------------- bars
def _candle(sym: str, t: datetime, o: float, c: float, vol: float = 100.0) -> CandleEvt:
    return CandleEvt(sym, int(t.timestamp() * 1000), o, max(o, c), min(o, c), c, vol, (o + c) / 2, 10)


def test_barbook_update_complete_gaps():
    book = bars_mod.BarBook()
    t0 = et_dt(D, time(9, 30))
    b = book.on_candle(_candle("SPY", t0, 650.0, 650.5))
    assert b is not None and not b.complete and book.count("SPY", t0, t0 + timedelta(hours=1), complete_only=False) == 1
    book.on_candle(_candle("SPY", t0, 650.0, 651.0, 250.0))  # forming-bar update, same minute
    assert book.bars[("SPY", t0)].close == 651.0 and book.bars[("SPY", t0)].updates == 2
    book.on_candle(_candle("SPY", t0 + timedelta(minutes=1), 651.0, 651.2))   # next minute → previous completes
    assert book.bars[("SPY", t0)].complete and not book.bars[("SPY", t0 + timedelta(minutes=1))].complete
    book.on_candle(_candle("SPY", t0 + timedelta(minutes=3), 651.2, 651.0))   # skipped minute 9:32
    assert [t.astimezone(ET).strftime("%H:%M") for t in book.gaps("SPY", t0, t0 + timedelta(minutes=4))] == ["09:32"]
    assert book.complete_before(t0 + timedelta(minutes=4, seconds=30)) == 1  # 9:33 bar finishes by 9:34:30
    assert book.count("SPY", t0, t0 + timedelta(minutes=4)) == 3
    rows = book.take_dirty()
    assert len(rows) == 3 and book.take_dirty() == [] and all(isinstance(r, BarRow) for r in rows)
    # snapshot replay of an old bar after reconnect must not un-complete it
    book.on_candle(_candle("SPY", t0, 650.0, 651.0, 250.0))
    assert book.bars[("SPY", t0)].complete
    # junk rejected
    assert book.on_candle(CandleEvt("SPY", 0, None, None, None, None, None, None, None)) is None
    assert book.on_candle(_candle("SPY", t0 + timedelta(seconds=30), 1, 1)) is None and book.rejected == 2
    assert candle_ticker("SPY{=1m,tho=true}") == "SPY" and candle_ticker("QQQ") == "QQQ"


def test_event_records_roundtrip():
    e = GreeksEvt(".SPY260928C650", 1_700_000_000_000, 1.2, 0.21, 0.5, 0.08, -0.3, 0.1)
    assert from_record(to_record(e)) == e
    q = QuoteEvt("SPY", 1, 649.9, 650.1)
    assert from_record(json.loads(json.dumps(to_record(q)))) == q


# ----------------------------------------------------------------------------- chains
def _raw_chain(spot: float = 650.0, step: float = 1.0, exps=(date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 2))) -> list[RawExpiration]:
    out = []
    for i, e in enumerate(exps):
        strikes = []
        k = spot - 60
        while k <= spot + 60:
            code = f"{e:%y%m%d}"
            strikes.append((k, f".SPY{code}C{k:g}", f".SPY{code}P{k:g}"))
            k += step
        out.append(RawExpiration(e, (e - D).days, strikes))
    return out


def test_plan_chain_expirations_and_window():
    plan = plan_chain("spy", _raw_chain(), 650.25, today=D, now_et=time(9, 20), n_exp=2, window_pct=3.0, max_per_side=20)
    assert plan.underlying == "SPY" and [e.expiration for e in plan.expirations] == [date(2026, 9, 28), date(2026, 9, 29)]
    assert plan.expirations[0].dte == 0 and plan.expirations[1].dte == 1
    ks = [s.strike for s in plan.expirations[0].strikes]
    assert ks == sorted(ks) and min(ks) >= 650.25 * 0.97 and max(ks) <= 650.25 * 1.03
    assert len([k for k in ks if k <= 650.25]) == 20 and len([k for k in ks if k > 650.25]) == 19   # window 630.7–669.8 → 631..669
    assert plan.n_strikes == 39 * 2 and len(plan.symbols()) == 39 * 2 * 2
    # after the close the same-day expiration is gone
    plan2 = plan_chain("SPY", _raw_chain(), 650.25, today=D, now_et=time(16, 5), n_exp=2, window_pct=3.0, max_per_side=20)
    assert [e.expiration for e in plan2.expirations] == [date(2026, 9, 29), date(2026, 10, 2)]
    # coarse strikes: window would hold < 3 → nearest per side regardless
    coarse = plan_chain("XYZ", _raw_chain(spot=100.0, step=25.0, exps=(date(2026, 10, 2),)), 101.0, today=D, now_et=time(9, 20), n_exp=1,
                        window_pct=3.0, max_per_side=4)
    assert [s.strike for s in coarse.expirations[0].strikes] == [40.0, 65.0, 90.0, 115.0, 140.0]   # chain spans 40–160 in $25 steps


def test_option_book_and_snapshot_summary():
    plan = plan_chain("SPY", _raw_chain(exps=(date(2026, 9, 28),)), 650.0, today=D, now_et=time(9, 20), n_exp=1, window_pct=1.0, max_per_side=3)
    book = OptionBook()
    ts = et_dt(D, time(10, 0))
    for s in plan.expirations[0].strikes:
        for sym, right in ((s.call, "C"), (s.put, "P")):
            itm = (s.strike < 650) if right == "C" else (s.strike > 650)
            book.on_quote(QuoteEvt(sym, 1, 1.0 + (0.5 if itm else 0), 1.1 + (0.5 if itm else 0)))
            book.on_greeks(GreeksEvt(sym, 1, 1.05, 0.20 + (0.01 if right == "P" else 0), 0.5 if right == "C" else -0.5, 0.05, -0.1, 0.1))
            book.on_summary(SummaryEvt(sym, 1, 1000 if right == "C" else 1500, None, None, None, None))
            book.on_trade(TradeEvt(sym, 1, 1.05, 1, 200 if right == "C" else 300))
    snap = build_snapshot(plan, book, 650.0, ts)
    assert snap["underlying"] == "SPY" and snap["spot"] == 650.0 and len(snap["expirations"]) == 1
    rows = snap["expirations"][0]["strikes"]
    assert len(rows) == 6 and len(rows[0]) == 15 and [r[0] for r in rows] == [648.0, 649.0, 650.0, 651.0, 652.0, 653.0]   # ATM counts as the "below" side
    summ = snap["summary"]
    assert summ["call_oi"] == 6000 and summ["put_oi"] == 9000 and summ["pc_oi"] == 1.5
    assert summ["call_vol"] == 1200 and summ["put_vol"] == 1800 and summ["n_options"] == 12
    assert summ["coverage"] == {"quotes": 1.0, "greeks": 1.0, "oi": 1.0}
    assert summ["atm_iv"] == pytest.approx(0.205, abs=1e-6)  # ATM strike 650: call 0.20, put 0.21
    g = snap["gamma"]
    assert g["regime"] == "negative" and g["coverage"] == 1.0   # puts have 1.5× the OI at equal gamma → net negative
    # missing data → None columns, not crashes
    empty = build_snapshot(plan, OptionBook(), None, ts)
    assert empty["summary"]["atm_iv"] is None and empty["gamma"]["regime"] == "unknown"
    assert all(v is None for v in empty["expirations"][0]["strikes"][0][1:])


# ------------------------------------------------------------------------------ gamma
def test_dealer_gamma_proxy_numbers():
    spot = 100.0
    # dollar gamma per 1% move: gamma × OI × 100 × spot² × 0.01 = 0.05 × 1000 × 100 × 10000 × 0.01 = 500,000
    assert dollar_gamma(0.05, 1000, spot) == pytest.approx(500_000.0)
    rows = [
        GexInput(95.0, "P", 0.02, 4000),   # put:  -0.02*4000*100*10000*0.01 = -800,000
        GexInput(100.0, "C", 0.05, 1000),  # call: +500,000
        GexInput(100.0, "P", 0.05, 1000),  # put:  -500,000
        GexInput(105.0, "C", 0.02, 6000),  # call: +1,200,000
        GexInput(110.0, "C", None, 100),   # no gamma → uncovered
    ]
    g = dealer_gamma_proxy(rows, spot)
    assert g["call_gex"] == 1_700_000 and g["put_gex"] == -1_300_000 and g["net_gex"] == 400_000
    assert g["regime"] == "positive" and g["call_wall"] == 105.0 and g["put_wall"] == 95.0
    # cumulative by strike: 95 → -800k, 100 → -800k, 105 → +400k ⇒ crossing between 100 and 105 at 100 + 5 × 800/(800+400)
    assert g["flip"] == pytest.approx(103.33, abs=0.01) and g["spot_vs_flip"] == "below"
    assert g["coverage"] == 0.8 and g["n"] == 5
    assert dealer_gamma_proxy([], spot)["regime"] == "unknown"
    assert dealer_gamma_proxy(rows, None)["regime"] == "unknown"
    only_puts = dealer_gamma_proxy([GexInput(95.0, "P", 0.02, 4000)], spot)
    assert only_puts["regime"] == "negative" and only_puts["call_wall"] is None and only_puts["flip"] is None


# ----------------------------------------------------------------------------- parsers
def test_parse_nasdaq_halts(fixtures: Path):
    rows = parse_halts_rss((fixtures / "nasdaq_halts_sample.xml").read_text(encoding="utf-8"))
    assert [r["symbol"] for r in rows] == ["ABCD", "EFGH"]          # malformed item skipped
    a, b = rows
    assert a["halt_time"] == "2026-09-28T13:45:12Z" and a["resumption_time"] == "2026-09-28T13:55:12Z"   # ET → UTC (EDT)
    assert a["reason_code"] == "LUDP" and a["market"] == "NASDAQ" and a["raw"]["pausethresholdprice"] == "12.34"
    assert b["reason_code"] == "T1" and b["resumption_time"] is None and b["halt_time"] == "2026-09-28T18:02:00Z"


def test_halt_from_profile():
    e = ProfileEvt("ABCD", 1_790_000_000_000, "HALTED", 1_790_000_000_000, None, "LUDP")
    h = halt_from_profile(e)
    assert h and h["symbol"] == "ABCD" and h["source"] == "dxlink_profile" and h["reason_code"] == "LUDP"
    assert halt_from_profile(ProfileEvt("ABCD", 1, "ACTIVE", None, None, None)) is None


def test_parse_cboe(fixtures: Path):
    doc = json.loads((fixtures / "cboe_vix_sample.json").read_text(encoding="utf-8"))
    assert parse_cboe_quote(doc) == 14.87
    assert parse_cboe_quote({"data": {"current_price": 0, "close": 0, "prev_day_close": 15.1}}) == 15.1
    assert parse_cboe_quote({}) is None and parse_cboe_quote(None) is None
    assert term_shape({"vix": 14.87, "vix3m": 17.93}) == "contango" and term_shape({"vix": 30, "vix3m": 25}) == "backwardation"
    assert term_shape({"vix": 14.87}) is None


# ------------------------------------------------------------------------------ store
def test_store_roundtrips(tmp_path: Path):
    st = Store(tmp_path / "s.sqlite")
    t0 = et_dt(D, time(9, 30))
    now = t0 + timedelta(minutes=2)
    rows = [BarRow("SPY", t0, 1, 2, 0.5, 1.5, 100, 1.2, 5, True), BarRow("SPY", t0 + timedelta(minutes=1), 1.5, 1.6, 1.4, 1.55, 50, 1.5, 3, False)]
    assert st.upsert_bars(rows, now) == 2
    dirty = st.dirty_bars()
    assert len(dirty) == 2 and dirty[0].payload()["bar_time"] == "2026-09-28T13:30:00Z"
    st.mark_bars_mirrored(dirty)
    assert st.dirty_bars() == []
    st.upsert_bars([BarRow("SPY", t0 + timedelta(minutes=1), 1.5, 1.7, 1.4, 1.65, 80, 1.5, 4, True)], now)   # update → dirty again
    assert len(st.dirty_bars()) == 1
    stats = st.bar_stats("SPY", t0, t0 + timedelta(minutes=5))
    assert stats == {"bars": 2, "complete": 2, "expected": 5, "missing": 3, "first_gap": "2026-09-28T13:32:00Z",
                     "first": "2026-09-28T13:30:00Z", "last": "2026-09-28T13:31:00Z"}
    sid = st.insert_snapshot(now, "SPY", 650.0, [{"exp": "2026-09-28", "dte": 0, "strikes": [[650, 1, 1.1]]}], {"atm_iv": 0.2}, {"regime": "positive"})
    assert st.dirty_snapshots()[0]["id"] == sid and st.snapshot_counts(t0, now) == {"SPY": 1}
    st.mark_snapshot_mirrored(sid)
    assert st.dirty_snapshots() == [] and st.latest_snapshot("SPY")["gamma"]["regime"] == "positive"
    st.insert_vix(now, {"vix": 14.9, "vix1d": 12.5, "vix9d": 12.8, "vix3m": 17.9, "errors": []})
    assert st.dirty_vix()[0]["vix"] == 14.9
    n = st.upsert_halts([{"symbol": "ABCD", "halt_time": "2026-09-28T13:45:12Z", "reason_code": "LUDP", "market": "NASDAQ", "resumption_time": None, "source": "nasdaq_rss", "raw": {"x": 1}}])
    assert n == 1 and len(st.dirty_halts()) == 1
    st.mark_halt_mirrored("ABCD", "2026-09-28T13:45:12Z")
    st.upsert_halts([{"symbol": "ABCD", "halt_time": "2026-09-28T13:45:12Z", "reason_code": "LUDP", "resumption_time": "2026-09-28T13:55:12Z"}])
    assert len(st.dirty_halts()) == 1          # resumption arrived → dirty again
    qid = st.enqueue_rpc("saa_log_run", {"p_job": "x"}, now)
    assert st.queue_size() == 1 and st.queued_rpcs()[0]["args"] == {"p_job": "x"}
    st.fail_rpc(qid, "boom")
    assert st.queued_rpcs()[0]["attempts"] == 1 and st.queued_rpcs()[0]["last_error"] == "boom"
    st.dequeue_rpc(qid)
    assert st.queue_size() == 0
    st.upsert_run("r1", "2026-09-28", now, "running", {"a": 1})
    st.upsert_run("r1", "2026-09-28", now, "done", {"a": 2}, ended_at=now)
    assert st.get_run("r1")["status"] == "done" and st.get_run("r1")["stats"] == {"a": 2}
    st.set_kv("k", "v")
    assert st.get_kv("k") == "v" and st.get_kv("nope", "d") == "d"
    st.log_event(now, "ERROR", "feed", "boom")
    assert len(st.error_events(t0)) == 1
    st.close()


# ----------------------------------------------------------------------------- mirror
class FakeHttp:
    """Emulates PostgREST + Cboe + Nasdaq + Telegram. Records every RPC call."""

    def __init__(self, fixtures: Path | None = None, fail_rpcs: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = fail_rpcs or set()
        self.fixtures = fixtures
        self.telegram: list[dict] = []
        self.down = False

    async def post_json(self, url, body, *, headers=None, timeout=15.0):
        if self.down:
            raise ConnectionError("network down")
        if "/rest/v1/rpc/" in url:
            fn = url.rsplit("/", 1)[-1]
            assert headers and headers["apikey"]
            if headers["apikey"].startswith("sb_"):
                assert "Authorization" not in headers          # new secret keys: apikey only (not a JWT)
            else:
                assert headers["apikey"] == headers["Authorization"].removeprefix("Bearer ")
            self.calls.append((fn, body))
            if fn in self.fail:
                return 500, {"message": f"{fn} exploded"}
            if fn == "saa_bars_upsert":
                return 200, len(body["p_rows"])
            if fn == "saa_chain_snapshot":
                return 200, len(self.calls)
            if fn == "saa_enqueue":
                return 200, len(self.calls)
            if fn == "saa_active_symbols":
                return 200, ["SPY", "QQQ", "IWM", "NVDA", "TSLA"]
            if fn == "saa_get_setting":
                return 200, "SPY,QQQ,IWM" if body.get("p_key") == "index_symbols" else None
            if fn == "saa_calendar_day":
                return 200, {"trade_date": "2026-09-28", "watch": ["NVDA"], "econ": [{"event": "Dallas Fed", "time_et": "10:30"}]}
            if fn == "saa_daemon_status":
                return 200, {"bars_today": 0}
            return 200, None
        if "api.telegram.org" in url:
            if "bad" in url:
                return 401, {"ok": False, "description": "Unauthorized"}
            self.telegram.append(body)
            return 200, {"ok": True, "result": {"message_id": 7}}
        return 404, {"message": "unknown"}

    async def get_json(self, url, *, headers=None, timeout=15.0):
        if self.down:
            raise ConnectionError("network down")
        assert self.fixtures is not None
        if "cdn.cboe.com" in url:
            doc = json.loads((self.fixtures / "cboe_vix_sample.json").read_text(encoding="utf-8"))
            bump = {"_VIX": 0, "_VIX1D": -2.36, "_VIX9D": -2.11, "_VIX3M": 3.06}[url.rsplit("/", 1)[-1].removesuffix(".json")]
            doc["data"]["current_price"] = round(14.87 + bump, 2)
            return doc
        raise AssertionError(url)

    async def get_text(self, url, *, headers=None, timeout=15.0):
        if self.down:
            raise ConnectionError("network down")
        assert self.fixtures is not None and "nasdaqtrader" in url
        return (self.fixtures / "nasdaq_halts_sample.xml").read_text(encoding="utf-8")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_mirror_flush_and_queue(env_file: Path, tmp_path: Path):
    s = config.load_settings(env_file, environ={}, state_dir=tmp_path)
    st = Store(tmp_path / "m.sqlite")
    http = FakeHttp()
    m = SupabaseMirror(s, st, http)
    assert m.enabled
    t0 = et_dt(D, time(9, 30))
    st.upsert_bars([BarRow("SPY", t0, 1, 2, 0.5, 1.5, 100, 1.2, 5, True)], t0)
    st.insert_snapshot(t0, "SPY", 650.0, [{"exp": "2026-09-28", "dte": 0, "strikes": []}], {"n_strikes": 0}, {"regime": "unknown"})
    st.insert_vix(t0, {"vix": 14.9, "vix1d": 12.5, "vix9d": 12.8, "vix3m": 17.9, "errors": []})
    st.upsert_halts([{"symbol": "ABCD", "halt_time": "2026-09-28T13:45:12Z", "reason_code": "LUDP", "source": "nasdaq_rss"}])
    m.queue("saa_log_run", {"p_job": "daemon:start", "p_ok": True, "p_detail": {}}, t0)
    out = await m.flush_all()
    assert out == {"queue": 1, "bars": 1, "snapshots": 1, "vix": 1, "halts": 1}
    fns = [c[0] for c in http.calls]
    assert fns == ["saa_log_run", "saa_bars_upsert", "saa_chain_snapshot", "saa_snapshot", "saa_halts_upsert"]
    bars_call = dict(http.calls)["saa_bars_upsert"]
    assert bars_call["p_rows"][0] == {"symbol": "SPY", "bar_time": "2026-09-28T13:30:00Z", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                                      "volume": 100.0, "vwap": 1.2, "trade_count": 5, "complete": True}
    snap_call = dict(http.calls)["saa_chain_snapshot"]
    assert snap_call["p_underlying"] == "SPY" and snap_call["p_expirations"] == [{"exp": "2026-09-28", "dte": 0, "strikes": []}]
    assert dict(http.calls)["saa_snapshot"]["p_kind"] == "vix_term"
    assert st.dirty_bars() == [] and st.dirty_snapshots() == [] and st.dirty_vix() == [] and st.dirty_halts() == [] and st.queue_size() == 0
    # outage: everything stays dirty/queued, nothing raises, then drains when back
    http.down = True
    st.upsert_bars([BarRow("SPY", t0 + timedelta(minutes=1), 1, 2, 0.5, 1.5, 100, 1.2, 5, False)], t0)
    m.queue("saa_log_run", {"p_job": "daemon:heartbeat", "p_ok": True}, t0)
    out = await m.flush_all()
    assert out["bars"] == -1 and out["queue"] == -1 and st.queue_size() == 1 and len(st.dirty_bars()) == 1 and m.failures >= 2
    assert "service-role-key-value-xyz" not in (m.last_error or "")
    http.down = False
    out = await m.flush_all()
    assert out["queue"] == 1 and out["bars"] == 1 and st.queue_size() == 0
    assert m.status()["pushed"] == {"bars": 2, "snapshots": 1, "vix": 1, "halts": 1, "queue": 2}
    # disabled mirror is a no-op everywhere
    s2 = config.load_settings(env_file, environ={"SAA_MIRROR": "false"}, state_dir=tmp_path)
    m2 = SupabaseMirror(s2, st, http)
    assert not m2.enabled and await m2.flush_all() == {"queue": 0, "bars": 0, "snapshots": 0, "vix": 0, "halts": 0}
    with pytest.raises(MirrorError):
        await m2.rpc("saa_log_run", {})


@pytest.mark.asyncio
async def test_notifier_paths(env_file: Path, tmp_path: Path):
    s = config.load_settings(env_file, environ={}, state_dir=tmp_path)
    st = Store(tmp_path / "n.sqlite")
    http = FakeHttp()
    m = SupabaseMirror(s, st, http)
    n = Notifier(s, m, http)
    t0 = et_dt(D, time(9, 25))
    assert await n.send("system", "hello", t0) == "outbox"
    assert http.calls[-1] == ("saa_enqueue", {"p_kind": "system", "p_text": "hello"})
    http.fail.add("saa_enqueue")                      # Supabase RPC failing → direct Telegram
    assert await n.send("system", "hello2", t0) == "telegram_direct"
    assert http.telegram[-1]["chat_id"] == "987654321" and http.telegram[-1]["text"] == "hello2"
    assert st.queue_size() == 1 and st.queued_rpcs()[0]["rpc"] == "saa_log_run"   # direct delivery logged for later
    http.down = True                                   # everything down → queued durably
    assert await n.send("system", "hello3", t0) == "queued"
    assert st.queued_rpcs()[-1] == {**st.queued_rpcs()[-1], "rpc": "saa_enqueue", "args": {"p_kind": "system", "p_text": "hello3"}}
    assert [x["path"] for x in n.sent] == ["outbox", "telegram_direct", "queued"]


# ---------------------------------------------------------------------------- reports
def test_report_texts_have_shape():
    hb = reports.heartbeat_text({
        "trade_date": D, "version": "0.2.0", "host": "mac", "broker": {"ok": True, "account_masked": "…1234", "account_type": "Margin", "options_level": "Basic"},
        "data": {"ok": True, "quote_token_ok": True, "env": "prod"}, "index_symbols": ["SPY", "QQQ", "IWM"], "single_names": ["NVDA"],
        "chains": {"n_index": 3, "n_single": 1, "exp_index": 2, "exp_single": 1, "n_options": 640},
        "vix": {"vix": 14.87, "vix1d": 12.51, "vix9d": 12.76, "vix3m": 17.93, "shape": "contango"},
        "gamma": {"regime": "positive", "flip": 648.5, "call_wall": 655.0, "put_wall": 640.0, "coverage": 0.97}, "halts": 0, "econ": ["Dallas Fed 10:30"], "mirror": True})
    lines = hb.splitlines()
    assert lines[0].startswith("SAA daemon ▸ Mon 2026-09-28 · v0.2.0 · mac") and len(lines) <= 10
    assert "sandbox ok (acct …1234, Margin, options Basic)" in hb and "prod DXLink ok" in hb and "VIX 14.9 · 1D 12.5" in hb and "(contango)" in hb
    assert "Gamma SPY pre-open: positive, flip 648.50, call wall 655.0, put wall 640.0, coverage 97%" in hb
    eod = reports.eod_text({
        "trade_date": "2026-09-28", "started": "09:20", "ended": "16:20", "unhandled": 0,
        "bars": {"SPY": {"complete": 390, "expected": 390, "missing": 0}, "IWM": {"complete": 389, "expected": 390, "missing": 1, "first_gap": "2026-09-28T16:07:00Z"}},
        "snapshots": {"SPY": 80, "QQQ": 80}, "snapshots_expected": 80, "n_options": 640,
        "gamma_open": {"regime": "positive", "flip": 648.5}, "gamma_close": {"regime": "negative", "flip": 651.2},
        "vix_open": {"vix": 14.9, "vix1d": 12.5, "vix3m": 17.9, "shape": "contango"}, "vix_close": {"vix": 15.3, "vix1d": 13.1, "vix3m": 18.0, "shape": "contango"},
        "halts": [{"symbol": "ABCD", "halt_time": "2026-09-28T13:45:12Z", "reason_code": "LUDP"}],
        "feed": {"events": 1234567, "reconnects": 1}, "errors": {"feed": 1},
        "mirror": {"enabled": True, "pushed": {"bars": 2345, "snapshots": 553, "vix": 79}, "queue": 0, "failures": 0},
        "telegram": [{"kind": "system", "path": "outbox"}]})
    assert "0 unhandled exceptions" in eod and "SPY 390/390" in eod and "IWM 389/390 (gap 12:07)" in eod
    assert "80 snapshots × 2 underlyings (expected 80)" in eod and "1,234,567 events · 1 reconnects · lag —s · errors caught: feed 1" in eod
    assert "Halts: 1 (ABCD 09:45 LUDP)" in eod and "Telegram: system via outbox" in eod


def test_mirror_headers_for_both_key_styles(env_file: Path, tmp_path: Path):
    legacy = config.load_settings(env_file, environ={"SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJIUzI1NiJ9.legacy.jwt"}, state_dir=tmp_path)
    h = SupabaseMirror(legacy, Store(tmp_path / "h1.sqlite"), FakeHttp())._headers()
    assert h["apikey"] == "eyJhbGciOiJIUzI1NiJ9.legacy.jwt" and h["Authorization"] == "Bearer eyJhbGciOiJIUzI1NiJ9.legacy.jwt"
    new = config.load_settings(env_file, environ={"SUPABASE_SERVICE_ROLE_KEY": "sb_secret_abcdefghijklmnop"}, state_dir=tmp_path)
    h = SupabaseMirror(new, Store(tmp_path / "h2.sqlite"), FakeHttp())._headers()
    assert h["apikey"] == "sb_secret_abcdefghijklmnop" and "Authorization" not in h
    alt = config.load_settings(env_file, environ={"SUPABASE_SERVICE_ROLE_KEY": "", "SUPABASE_SECRET_KEY": "sb_secret_zzzzzzzzzzzzzzzz"}, state_dir=tmp_path)
    assert alt.supabase_key.value == "sb_secret_zzzzzzzzzzzzzzzz"     # either variable name is accepted
