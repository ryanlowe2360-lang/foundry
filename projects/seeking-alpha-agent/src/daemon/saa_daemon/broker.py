"""tastytrade sessions. Two logins, two jobs:

* **data** — production OAuth (TT_PROD_*): market data only (DXLink quote tokens, option chains,
  REST market-data). The sandbox has no market data (D11), so this is where quotes come from.
* **broker** — sandbox OAuth (TT_SANDBOX_*): the paper account. M2–M3 only read it (account number, options level)
  for the heartbeat; M4 hands the session to `execution.tastytrade_broker.TastytradeBroker` for sandbox orders.

Order code lives only in `saa_daemon/execution/`. Production brokerage is refused by config until M5.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .log import quiet_sdk_loggers

log = logging.getLogger("saa.broker")


def mask_account(acct: str | None) -> str | None:
    if not acct:
        return None
    return f"…{acct[-4:]}" if len(acct) > 4 else "…"


@dataclass
class SessionInfo:
    env: str
    ok: bool = False
    error: str | None = None
    account_masked: str | None = None
    account_type: str | None = None
    options_level: str | None = None
    quote_token_ok: bool | None = None
    quote_level: str | None = None     # tastytrade's entitlement label on the DXLink token


@dataclass
class Brokerage:
    settings: Settings
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    data: Any = None            # tastytrade.Session (production) or None
    broker: Any = None          # tastytrade.Session (sandbox) or None
    data_info: SessionInfo = field(default_factory=lambda: SessionInfo("prod"))
    broker_info: SessionInfo = field(default_factory=lambda: SessionInfo("sandbox"))
    _rest_forbidden: bool = False   # set after a 403 from /market-data (D15)

    async def open(self) -> "Brokerage":
        from tastytrade import Session  # imported here so the rest of the package is SDK-free

        quiet_sdk_loggers()  # the SDK forces its logger to DEBUG at import time
        s = self.settings
        self.data_info = SessionInfo(s.data_env)
        self.broker_info = SessionInfo(s.broker_env)

        async def login(env: str, info: SessionInfo) -> Any:
            missing = s.missing_tt_keys(env)
            if missing:
                info.error = f"missing {', '.join(missing)}"
                return None
            _cid, secret, token = s.tt_credentials(env)
            try:
                sess = Session(provider_secret=secret.value, refresh_token=token.value, is_test=(env == "sandbox"), timeout=20.0)
                await self.stack.enter_async_context(sess)
                await sess.refresh(force=True)
                info.ok = True
                return sess
            except Exception as e:  # noqa: BLE001
                info.error = f"{type(e).__name__}: {str(e)[:160]}"
                return None

        self.data = await login(s.data_env, self.data_info)
        self.broker = await login(s.broker_env, self.broker_info)

        if self.data is not None:
            try:
                tok = await self.data._get("/api-quote-tokens")
                self.data_info.quote_token_ok = bool(tok.get("token")) and bool(tok.get("dxlink-url"))
                self.data_info.quote_level = str(tok.get("level")) if tok.get("level") is not None else None
            except Exception as e:  # noqa: BLE001
                self.data_info.quote_token_ok = False
                self.data_info.error = f"quote token: {type(e).__name__}: {str(e)[:120]}"
        if self.broker is not None:
            try:
                from tastytrade import Account

                accts = await Account.get(self.broker)
                acct = accts[0] if isinstance(accts, list) and accts else None
                if acct is not None:
                    self.broker_info.account_masked = mask_account(acct.account_number)
                    self.broker_info.account_type = f"{acct.margin_or_cash}"
                    self.broker_info.options_level = getattr(acct, "suitable_options_level", None)
                else:
                    self.broker_info.error = "no open accounts"
            except Exception as e:  # noqa: BLE001
                self.broker_info.error = f"accounts: {type(e).__name__}: {str(e)[:120]}"
        log.info("data session (%s): %s%s", self.data_info.env, "ok" if self.data_info.ok else "FAILED",
                 f" — {self.data_info.error}" if self.data_info.error else "")
        log.info("broker session (%s): %s%s", self.broker_info.env, "ok" if self.broker_info.ok else "FAILED",
                 f" — {self.broker_info.error}" if self.broker_info.error else "")
        return self

    async def close(self) -> None:
        await self.stack.aclose()

    # ------------------------------------------------------------- data helpers
    @staticmethod
    def _px(item: dict[str, Any]) -> float | None:
        """Best spot from a raw tastytrade market-data item (dasherized keys): mark → last → mid → close → prev-close."""
        for k in ("mark", "last", "mid", "close", "prev-close", "prev-day-close"):
            v = item.get(k)
            if v not in (None, "", "NaN"):
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f > 0:
                    return f
        return None

    async def _rest_json(self, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        """Raw GET on the data session; never raises on a non-2xx (returns status + parsed body or text)."""
        await self.data.refresh()
        r = await self.data._client.get(path, params=params)
        try:
            body: Any = r.json()
        except Exception:  # noqa: BLE001
            body = r.text
        return r.status_code, body

    async def _spots_rest_by_type(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        eq = [s for s in symbols if self.settings.rest_instrument_kind(s) == "equity"]
        ix = [s.lstrip("$") for s in symbols if self.settings.rest_instrument_kind(s) == "index"]
        params: dict[str, Any] = {}
        if eq:
            params["equity"] = eq[:100]
        if ix:
            params["index"] = ix[:100]
        if not params:
            return out
        status, body = await self._rest_json("/market-data/by-type", params)
        items = body.get("data", {}).get("items") if isinstance(body, dict) else None
        if status // 100 != 2 or not isinstance(items, list):
            log.warning("market-data/by-type → HTTP %s: %s", status, str(body)[:200])
            if status == 403:
                self._note_rest_forbidden()
            return out
        for it in items:
            px = self._px(it)
            if px and it.get("symbol"):
                out[str(it["symbol"]).upper()] = px
        return out

    async def _spots_rest_single(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in symbols:
            kind = self.settings.rest_instrument_kind(s)
            status, body = await self._rest_json(f"/market-data/{kind}/{s.lstrip('$')}")
            data = body.get("data") if isinstance(body, dict) else None
            px = self._px(data) if isinstance(data, dict) else None
            if px:
                out[s] = px
            else:
                log.warning("market-data/%s/%s → HTTP %s: %s", kind, s, status, str(body)[:160])
                if status == 403:
                    self._note_rest_forbidden()
                    break
                if status == 429:
                    break
        return out

    def _note_rest_forbidden(self) -> None:
        if not self._rest_forbidden:
            self._rest_forbidden = True
            log.warning("REST market data is not entitled on this OAuth app (403) — using DXLink for spots for the rest of this run")

    async def _spots_dxlink(self, symbols: list[str], window: float = 6.0) -> dict[str, float]:
        """Mid of the first DXLink quote per symbol — works whenever the streamer does (weekends included)."""
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Quote

        out: dict[str, float] = {}
        want = set(symbols)
        async with DXLinkStreamer(self.data) as st:
            await st.subscribe(Quote, sorted(want))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + window
            while want and loop.time() < deadline:
                try:
                    q = await asyncio.wait_for(st.get_event(Quote), timeout=max(0.05, deadline - loop.time()))
                except TimeoutError:
                    break
                sym = q.event_symbol
                if sym in want and q.bid_price and q.ask_price:
                    out[sym] = float((q.bid_price + q.ask_price) / 2)
                    want.discard(sym)
        return out

    async def spot_prices(self, symbols: list[str]) -> dict[str, float]:
        """Spot per underlying: DXLink quote mid first (the entitlement that is known to work — REST market data returned
        403 on Ryan's production OAuth app, D15), then REST by-type, then REST per symbol. Each step only fills what the
        previous one missed and logs failures (status + body) instead of raising, so a chain plan is never lost to one bad
        call. After a 403 from REST the REST steps are skipped for the rest of the process (avoids a 429 storm)."""
        if self.data is None or not symbols:
            return {}
        symbols = list(dict.fromkeys(s.upper() for s in symbols))
        out: dict[str, float] = {}
        steps = [("dxlink", self._spots_dxlink)]
        if not self._rest_forbidden:
            steps += [("rest_by_type", self._spots_rest_by_type), ("rest_single", self._spots_rest_single)]
        for name, fn in steps:
            missing = [s for s in symbols if s not in out]
            if not missing:
                break
            if name.startswith("rest") and self._rest_forbidden:
                continue
            try:
                got = await fn(missing)
                out.update(got)
                if got:
                    log.info("spots via %s: %s", name, ", ".join(f"{k} {v:.2f}" for k, v in sorted(got.items())))
            except Exception as e:  # noqa: BLE001
                log.warning("spots via %s failed: %s: %s", name, type(e).__name__, str(e)[:160])
        still = [s for s in symbols if s not in out]
        if still:
            log.error("no spot price for %s after all methods", " ".join(still))
        return out

    async def nested_chain(self, underlying: str) -> Any:
        """NestedOptionChain for the standard root (skips non-standard roots such as SPXW-style adjusted chains)."""
        from tastytrade.instruments import NestedOptionChain

        chains = await NestedOptionChain.get(self.data, underlying)
        if not chains:
            return None
        std = [c for c in chains if c.option_chain_type.lower().startswith("standard")]
        return (std or chains)[0]
