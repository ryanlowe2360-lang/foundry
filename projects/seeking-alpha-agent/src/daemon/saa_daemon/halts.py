"""Nasdaq Trader trade-halts RSS poller (all US listings, not only Nasdaq-listed) plus a DXLink Profile
handler. Parser is namespace-agnostic so a feed tweak does not break it; tested on a fixture."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .clock import UTC
from .events import ProfileEvt
from .http import HttpClient

NASDAQ_HALTS_RSS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
_ET = ZoneInfo("America/New_York")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _et_ts(d: str | None, t: str | None) -> datetime | None:
    """'09/28/2026' + '09:45:12' (ET) → aware UTC datetime."""
    if not d or not t:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(f"{d.strip()} {t.strip()}", fmt).replace(tzinfo=_ET).astimezone(UTC)
        except ValueError:
            continue
    return None


def parse_halts_rss(xml_text: str) -> list[dict[str, Any]]:
    """RSS → [{symbol, halt_time (ISO UTC), reason_code, market, resumption_time, source, raw}]."""
    root = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        f: dict[str, str] = {}
        for child in item:
            f[_local(child.tag)] = (child.text or "").strip()
        sym = f.get("issuesymbol") or f.get("symbol")
        halt = _et_ts(f.get("haltdate"), f.get("halttime"))
        if not sym or halt is None:
            continue
        res = _et_ts(f.get("resumptiondate"), f.get("resumptiontradetime")) or _et_ts(f.get("resumptiondate"), f.get("resumptionquotetime"))
        out.append({
            "symbol": sym.upper(),
            "halt_time": halt.isoformat().replace("+00:00", "Z"),
            "reason_code": f.get("reasoncode") or None,
            "market": f.get("market") or None,
            "resumption_time": res.isoformat().replace("+00:00", "Z") if res else None,
            "source": "nasdaq_rss",
            "raw": {k: v for k, v in f.items() if k in ("issuename", "pausethresholdprice", "resumptionquotetime", "resumptiontradetime", "reasoncode")},
        })
    return out


async def fetch_halts(http: HttpClient) -> list[dict[str, Any]]:
    text = await http.get_text(NASDAQ_HALTS_RSS, headers={"Accept": "application/rss+xml, application/xml, text/xml"}, timeout=15.0)
    return parse_halts_rss(text)


def halt_from_profile(e: ProfileEvt) -> dict[str, Any] | None:
    """DXLink Profile with trading_status HALTED → a halt row (second, faster signal for our own symbols)."""
    if not e.trading_status or e.trading_status.upper() != "HALTED":
        return None
    start_ms = e.halt_start_ms or e.time_ms
    if not start_ms:
        return None
    halt = datetime.fromtimestamp(start_ms / 1000.0, tz=UTC)
    res = datetime.fromtimestamp(e.halt_end_ms / 1000.0, tz=UTC) if e.halt_end_ms else None
    return {"symbol": e.symbol.upper(), "halt_time": halt.isoformat().replace("+00:00", "Z"), "reason_code": e.status_reason,
            "market": None, "resumption_time": res.isoformat().replace("+00:00", "Z") if res else None, "source": "dxlink_profile",
            "raw": {"status_reason": e.status_reason}}
