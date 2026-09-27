"""Thin async HTTP wrapper (httpx2, already a tastytrade dependency) with a fake-able interface."""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

log = logging.getLogger("saa.http")


class HttpClient(Protocol):
    async def get_text(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> str: ...
    async def get_json(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Any: ...
    async def post_json(self, url: str, body: Any, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> tuple[int, Any]: ...
    async def aclose(self) -> None: ...


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class Httpx2Client:
    """`verify` follows httpx2: True = the system trust store (truststore), or a CA-bundle path / SSLContext."""

    def __init__(self, user_agent: str = "seeking-alpha-agent-daemon/0.2", verify: Any = True):
        import httpx2  # local import so tests without the SDK stack can still import this module

        self._c = httpx2.AsyncClient(headers={"User-Agent": user_agent}, follow_redirects=True, verify=verify)

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> str:
        r = await self._c.get(url, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise HttpError(r.status_code, url, r.text)
        return r.text

    async def get_json(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Any:
        text = await self.get_text(url, headers=headers, timeout=timeout)
        return json.loads(text)

    async def post_json(self, url: str, body: Any, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> tuple[int, Any]:
        h = {"Content-Type": "application/json", **(headers or {})}
        r = await self._c.post(url, content=json.dumps(body, default=str), headers=h, timeout=timeout)
        text = r.text
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = text
        return r.status_code, data

    async def aclose(self) -> None:
        await self._c.aclose()


def certifi_client() -> Httpx2Client | None:
    """A second client that trusts Mozilla's CA bundle (certifi) instead of the OS trust store. Used only as a
    fallback when a public CDN (Cboe) fails TLS verification through the system store — seen intermittently on macOS
    when an edge node serves a chain without its intermediate certificate."""
    try:
        import certifi
    except ImportError:
        log.debug("certifi not installed — no TLS fallback client")
        return None
    try:
        return Httpx2Client(verify=certifi.where())
    except Exception as e:  # noqa: BLE001
        log.debug("certifi client unavailable: %s", e)
        return None
