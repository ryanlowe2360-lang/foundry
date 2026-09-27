"""Telegram delivery. Primary path (spec): a row in saa.outbox via `saa_enqueue` — the outbox insert
trigger hands it to the `telegram-send` edge function immediately. Fallback when Supabase is unreachable:
the Bot API directly from this machine (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env). If both fail the
enqueue is queued durably and retried by the mirror loop, so a heartbeat is delayed, never lost."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .config import Settings
from .http import HttpClient
from .mirror import MirrorError, SupabaseMirror

log = logging.getLogger("saa.telegram")


class Notifier:
    def __init__(self, settings: Settings, mirror: SupabaseMirror, http: HttpClient | None):
        self.settings = settings
        self.mirror = mirror
        self.http = http
        self.sent: list[dict[str, Any]] = []   # {kind, path, at, ok}

    async def send(self, kind: str, text: str, now: datetime) -> str:
        """Returns the path used: 'outbox' | 'telegram_direct' | 'queued' | 'dropped'."""
        text = text.strip()
        if self.mirror.enabled:
            try:
                await self.mirror.rpc("saa_enqueue", {"p_kind": kind, "p_text": text})
                return self._done(kind, "outbox", now, True)
            except MirrorError as e:
                log.warning("outbox enqueue failed (%s); trying direct Telegram", e)
        if self.settings.telegram_fallback_configured and self.http is not None:
            try:
                await self._direct(text)
                # still record the message in the outbox once Supabase is back (kind marks it delivered directly)
                self.mirror.queue("saa_log_run", {"p_job": "daemon:telegram_direct", "p_ok": True,
                                                  "p_detail": {"kind": kind, "chars": len(text)}}, now)
                return self._done(kind, "telegram_direct", now, True)
            except Exception as e:  # noqa: BLE001
                log.error("direct Telegram failed: %s: %s", type(e).__name__, str(e)[:160])
        if self.mirror.enabled:
            self.mirror.queue("saa_enqueue", {"p_kind": kind, "p_text": text}, now)
            return self._done(kind, "queued", now, False)
        log.error("no Telegram path available; message dropped (%s, %d chars)", kind, len(text))
        return self._done(kind, "dropped", now, False)

    async def _direct(self, text: str) -> None:
        assert self.http is not None
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token.value}/sendMessage"
        status, data = await self.http.post_json(url, {"chat_id": self.settings.telegram_chat_id, "text": text[:4000],
                                                       "disable_web_page_preview": True}, timeout=15.0)
        if status >= 400 or not (isinstance(data, dict) and data.get("ok")):
            desc = data.get("description") if isinstance(data, dict) else str(data)
            raise RuntimeError(f"Telegram HTTP {status}: {str(desc)[:120]}")

    def _done(self, kind: str, path: str, now: datetime, ok: bool) -> str:
        self.sent.append({"kind": kind, "path": path, "at": now.isoformat(), "ok": ok})
        log.info("telegram %s via %s", kind, path)
        return path
