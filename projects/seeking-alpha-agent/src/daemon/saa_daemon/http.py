"""Thin async HTTP wrapper (httpx2, already a tastytrade dependency) with a fake-able interface."""
from __future__ import annotations

import json
from typing import Any, Protocol


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
    def __init__(self, user_agent: str = "seeking-alpha-agent-daemon/0.2"):
        import httpx2  # local import so tests without the SDK stack can still import this module

        self._c = httpx2.AsyncClient(headers={"User-Agent": user_agent}, follow_redirects=True)

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
