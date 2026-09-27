"""Cboe VIX term structure (VIX, VIX1D, VIX9D, VIX3M) from the same delayed-quotes CDN endpoint the M1
`market-data` edge function uses. Parsing is separate from fetching so it can be tested on fixtures."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from .http import HttpClient

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes"
TERM = (("vix", "_VIX"), ("vix1d", "_VIX1D"), ("vix9d", "_VIX9D"), ("vix3m", "_VIX3M"))


def parse_cboe_quote(doc: Any) -> float | None:
    data = (doc or {}).get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        return None
    for k in ("current_price", "close", "prev_day_close"):
        v = data.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def term_shape(term: dict[str, Any]) -> str | None:
    """'contango' when VIX < VIX3M (calm), 'backwardation' when VIX > VIX3M (stress), else None."""
    v, v3 = term.get("vix"), term.get("vix3m")
    if v is None or v3 is None:
        return None
    return "contango" if v < v3 else "backwardation" if v > v3 else "flat"


async def fetch_vix_term(http: HttpClient, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"fetched_at": now.isoformat(), "errors": []}

    async def one(key: str, sym: str) -> None:
        try:
            out[key] = parse_cboe_quote(await http.get_json(f"{CBOE}/{sym}.json", timeout=12.0))
            if out[key] is None:
                out["errors"].append(f"cboe {sym}: empty")
        except Exception as e:  # noqa: BLE001 - recorded, never raised
            out[key] = None
            out["errors"].append(f"cboe {sym}: {type(e).__name__}: {str(e)[:120]}")

    await asyncio.gather(*(one(k, s) for k, s in TERM))
    out["shape"] = term_shape(out)
    return out
