"""Regression tests from the first real smoke run on Ryan's Mac (2026-09-27 17:29 ET):
1. `/market-data/by-type` came back non-2xx with a string `error` and the SDK's parser crashed → spots are now fetched
   through a fallback chain (REST by-type → REST per symbol → DXLink quote mid) that logs the raw failure instead.
2. One of four Cboe fetches failed TLS verification while the others succeeded → per-symbol retries + a certifi client.
3. The SDK forces its logger to DEBUG at import → held at WARNING by level reset + a handler-level filter."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from saa_daemon import config
from saa_daemon.broker import Brokerage
from saa_daemon.log import QuietSdkFilter, quiet_sdk_loggers, setup_logging
from saa_daemon.vix import fetch_vix_term


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else "json"

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _FakeSession:
    """Mimics the parts of tastytrade.Session that Brokerage._rest_json touches."""

    def __init__(self, routes):
        self.routes = routes           # path → (status, body) or callable(params)
        self.calls: list[tuple[str, dict | None]] = []
        self._client = self

    async def refresh(self, force: bool = False) -> None:
        pass

    async def get(self, path, params=None):
        self.calls.append((path, params))
        r = self.routes.get(path)
        if callable(r):
            r = r(params)
        return _Resp(*r) if r else _Resp(404, {"error": "no route"})


def _brokerage(env_file: Path, tmp_path: Path, session) -> Brokerage:
    s = config.load_settings(env_file, environ={}, state_dir=tmp_path)
    b = Brokerage(s)
    b.data = session
    return b


@pytest.mark.asyncio
async def test_spot_chain_by_type_string_error_falls_back_to_single(env_file: Path, tmp_path: Path, caplog):
    routes = {
        "/market-data/by-type": (400, {"error": "Bad Request: unsupported query"}),           # what killed the SDK parser
        "/market-data/equity/SPY": (200, {"data": {"symbol": "SPY", "mark": "771.89", "last": "771.7"}}),
        "/market-data/equity/QQQ": (200, {"data": {"symbol": "QQQ", "mark": None, "last": None, "mid": "580.10"}}),
        "/market-data/equity/IWM": (200, {"data": {"symbol": "IWM", "prev-close": "240.55"}}),
    }
    sess = _FakeSession(routes)
    b = _brokerage(env_file, tmp_path, sess)
    b._spots_dxlink = lambda syms, window=6.0: (_ for _ in ()).throw(AssertionError("dxlink should not be needed"))  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger="saa.broker"):
        out = await b.spot_prices(["SPY", "QQQ", "IWM"])
    assert out == {"SPY": 771.89, "QQQ": 580.10, "IWM": 240.55}
    assert any("market-data/by-type → HTTP 400" in r.getMessage() and "unsupported query" in r.getMessage() for r in caplog.records)
    assert [c[0] for c in sess.calls][:1] == ["/market-data/by-type"] and len(sess.calls) == 4


@pytest.mark.asyncio
async def test_spot_chain_by_type_success_and_dxlink_last_resort(env_file: Path, tmp_path: Path):
    routes = {
        "/market-data/by-type": (200, {"data": {"items": [{"symbol": "SPY", "mark": "771.89"}, {"symbol": "QQQ", "mark": "NaN", "last": "580.2"}]}}),
        "/market-data/equity/IWM": (500, "upstream timeout"),
    }
    sess = _FakeSession(routes)
    b = _brokerage(env_file, tmp_path, sess)
    dx_calls: list[list[str]] = []

    async def fake_dx(syms, window=6.0):
        dx_calls.append(list(syms))
        return {"IWM": 240.6}

    b._spots_dxlink = fake_dx  # type: ignore[assignment]
    out = await b.spot_prices(["SPY", "QQQ", "IWM", "spy"])          # duplicate/lower-case input is normalised
    assert out == {"SPY": 771.89, "QQQ": 580.2, "IWM": 240.6}
    assert dx_calls == [["IWM"]]                                        # only the still-missing symbol went to DXLink
    assert sess.calls[0][1] == {"equity": ["SPY", "QQQ", "IWM"]}       # by-type asked for all three, equities only


@pytest.mark.asyncio
async def test_spot_chain_everything_fails_is_empty_not_exception(env_file: Path, tmp_path: Path, caplog):
    sess = _FakeSession({})
    b = _brokerage(env_file, tmp_path, sess)

    async def boom(syms, window=6.0):
        raise ConnectionError("socket closed")

    b._spots_dxlink = boom  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger="saa.broker"):
        out = await b.spot_prices(["SPY"])
    assert out == {}
    assert any("no spot price for SPY after all three methods" in r.getMessage() for r in caplog.records)


def test_px_parsing():
    assert Brokerage._px({"mark": "0", "last": "NaN", "mid": None, "close": "12.5"}) == 12.5
    assert Brokerage._px({"mark": "x"}) is None and Brokerage._px({}) is None


class _FlakyCboe:
    """Fails the first `fail_n` calls for a given symbol, then succeeds; `always_fail` symbols never succeed."""

    def __init__(self, fail_n: int, always_fail: set[str] | None = None):
        self.fail_n = fail_n
        self.always = always_fail or set()
        self.calls: dict[str, int] = {}

    async def get_json(self, url, *, headers=None, timeout=15.0):
        sym = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.calls[sym] = self.calls.get(sym, 0) + 1
        if sym in self.always or self.calls[sym] <= self.fail_n:
            raise ConnectionError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        return {"data": {"current_price": {"_VIX": 14.87, "_VIX1D": 12.51, "_VIX9D": 12.76, "_VIX3M": 17.93}[sym]}}


class _GoodCboe(_FlakyCboe):
    def __init__(self):
        super().__init__(0)


@pytest.mark.asyncio
async def test_vix_retries_then_fallback_client():
    now = datetime(2026, 9, 28, 13, 25, tzinfo=timezone.utc)
    flaky = _FlakyCboe(fail_n=2)                       # third attempt succeeds → no fallback needed
    term = await fetch_vix_term(flaky, now, attempts=3, retry_delay=0)
    assert term["vix"] == 14.87 and term["vix3m"] == 17.93 and term["errors"] == [] and "notes" not in term
    assert flaky.calls["_VIX"] == 3
    hard = _FlakyCboe(fail_n=0, always_fail={"_VIX"})  # primary never works for _VIX → certifi client used once
    term = await fetch_vix_term(hard, now, fallback=_GoodCboe(), attempts=3, retry_delay=0)
    assert term["vix"] == 14.87 and term["errors"] == [] and term["notes"] == ["_VIX: via certifi fallback"] and term["shape"] == "contango"
    assert hard.calls["_VIX"] == 3
    term = await fetch_vix_term(hard, now, fallback=None, attempts=2, retry_delay=0)   # no fallback → recorded, not raised
    assert term["vix"] is None and term["errors"] == ["cboe _VIX: ConnectionError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (after 2 tries)"]
    assert term["vix1d"] == 12.51


def test_sdk_logger_is_quiet_after_setup(tmp_path: Path):
    setup_logging("INFO", [], None)
    import tastytrade  # noqa: F401  (its __init__ sets DEBUG)

    quiet_sdk_loggers()
    assert logging.getLogger("tastytrade").level == logging.WARNING
    f = QuietSdkFilter()
    rec = logging.LogRecord("tastytrade", logging.DEBUG, __file__, 1, "received: frame", (), None)
    assert f.filter(rec) is False
    rec = logging.LogRecord("tastytrade", logging.WARNING, __file__, 1, "reconnecting", (), None)
    assert f.filter(rec) is True
    rec = logging.LogRecord("saa.daemon", logging.DEBUG, __file__, 1, "ours", (), None)
    assert f.filter(rec) is True
    root = logging.getLogger()
    assert all(any(isinstance(x, QuietSdkFilter) for x in h.filters) for h in root.handlers)
