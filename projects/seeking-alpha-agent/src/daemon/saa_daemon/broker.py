"""tastytrade sessions. Two logins, two jobs:

* **data** — production OAuth (TT_PROD_*): market data only (DXLink quote tokens, option chains,
  REST market-data). The sandbox has no market data (D11), so this is where quotes come from.
* **broker** — sandbox OAuth (TT_SANDBOX_*): the account the M4 paper orders will go to. In M2 it is
  only logged into and read (account number, options level) for the heartbeat.

No order code lives anywhere in this package. Production brokerage is refused by config until M5.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .config import Settings

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


@dataclass
class Brokerage:
    settings: Settings
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    data: Any = None            # tastytrade.Session (production) or None
    broker: Any = None          # tastytrade.Session (sandbox) or None
    data_info: SessionInfo = field(default_factory=lambda: SessionInfo("prod"))
    broker_info: SessionInfo = field(default_factory=lambda: SessionInfo("sandbox"))

    async def open(self) -> "Brokerage":
        from tastytrade import Session  # imported here so the rest of the package is SDK-free

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
    async def spot_prices(self, symbols: list[str]) -> dict[str, float]:
        """REST market data (mark, else last, else mid) for the given underlyings, 100 per call."""
        if self.data is None or not symbols:
            return {}
        from tastytrade.market_data import get_market_data_by_type

        out: dict[str, float] = {}
        eq = [s for s in symbols if self.settings.rest_instrument_kind(s) == "equity"]
        ix = [s.lstrip("$") for s in symbols if self.settings.rest_instrument_kind(s) == "index"]
        for i in range(0, max(len(eq), 1), 100):
            chunk_eq = eq[i:i + 100]
            chunk_ix = ix if i == 0 else []
            if not chunk_eq and not chunk_ix:
                break
            items = await get_market_data_by_type(self.data, equities=chunk_eq or None, indices=chunk_ix or None)
            for m in items:
                px = m.mark or m.last or m.mid or m.close or m.prev_close
                if px:
                    out[str(m.symbol).upper()] = float(px)
        return out

    async def nested_chain(self, underlying: str) -> Any:
        """NestedOptionChain for the standard root (skips non-standard roots such as SPXW-style adjusted chains)."""
        from tastytrade.instruments import NestedOptionChain

        chains = await NestedOptionChain.get(self.data, underlying)
        if not chains:
            return None
        std = [c for c in chains if c.option_chain_type.lower().startswith("standard")]
        return (std or chains)[0]
