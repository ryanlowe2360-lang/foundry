"""Telegram from the daemon, directly on the Bot API (the outbox path cannot carry inline buttons or receive taps).

* `send` / `edit` — proposals with **Approve / Skip** buttons and their decision edits.
* `poll` — long-polls `getUpdates` for button taps (`callback_query`) and commands (`/halt`, `/resume`, `/status`, …).

Only one consumer may poll a bot's updates. The M1 `telegram-send` edge function also polls (every 2 minutes and on
every outbox insert) — it is changed in M4 to **skip polling while the daemon is alive** (`saa.settings.daemon_last_seen`
within 3 minutes). The daemon loads the shared offset from `saa.settings.telegram_update_offset` when it starts polling
and writes it back after every batch, so the hand-over in both directions is clean. Only the owner's chat is honoured.
The bot token is read from `Settings` only when a URL is built; it is never logged (the log filter redacts it anyway).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ..clock import Clock
from ..config import Settings
from ..http import HttpClient

log = logging.getLogger("saa.telegram_bot")

OnCallback = Callable[[str, str, str, int | None], Awaitable[str | None]]   # (data, from_name, callback_id, message_id) → answer text
OnCommand = Callable[[str, str, str], Awaitable[str | None]]              # (command, args, from_name) → reply text


class TelegramConflict(Exception):
    """409: another getUpdates consumer is active (or a webhook is set)."""


class TelegramBot:
    def __init__(self, settings: Settings, http: HttpClient | None, clock: Clock, *, poll_timeout_s: int = 20):
        self.settings = settings
        self.http = http
        self.clock = clock
        self.poll_timeout_s = int(poll_timeout_s)
        self.sent = 0
        self.edited = 0
        self.polls = 0
        self.updates_seen = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_update_id: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_fallback_configured) and self.http is not None

    @property
    def chat_id(self) -> str:
        return str(self.settings.telegram_chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token.value}/{method}"

    async def _call(self, method: str, body: dict[str, Any], *, timeout: float = 15.0) -> Any:
        if not self.configured:
            raise RuntimeError("Telegram bot not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        assert self.http is not None
        status, data = await self.http.post_json(self._url(method), body, timeout=timeout)
        if status == 409:
            raise TelegramConflict(str((data or {}).get("description") if isinstance(data, dict) else data)[:160])
        if status >= 400 or not (isinstance(data, dict) and data.get("ok")):
            desc = data.get("description") if isinstance(data, dict) else str(data)
            raise RuntimeError(f"Telegram {method} HTTP {status}: {str(desc)[:160]}")
        return data.get("result")

    # ------------------------------------------------------------------------------------------------ messages
    async def send(self, text: str, *, buttons: list[tuple[str, str]] | None = None) -> int | None:
        body: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": label, "callback_data": data[:64]} for label, data in buttons]]}
        res = await self._call("sendMessage", body)
        self.sent += 1
        return int(res["message_id"]) if isinstance(res, dict) and res.get("message_id") is not None else None

    async def edit(self, message_id: int, text: str) -> None:
        try:
            await self._call("editMessageText", {"chat_id": self.chat_id, "message_id": int(message_id), "text": text[:4000], "disable_web_page_preview": True})
            self.edited += 1
        except RuntimeError as e:
            if "not modified" not in str(e):
                raise

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        body: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            body["text"] = text[:200]
        try:
            await self._call("answerCallbackQuery", body)
        except RuntimeError as e:
            log.debug("answerCallbackQuery failed: %s", e)

    # ------------------------------------------------------------------------------------------------- polling
    async def get_updates(self, offset: int, *, timeout_s: int | None = None) -> list[dict[str, Any]]:
        t = self.poll_timeout_s if timeout_s is None else int(timeout_s)
        res = await self._call("getUpdates", {"offset": int(offset), "timeout": t, "allowed_updates": ["message", "callback_query"]}, timeout=float(t + 10))
        self.polls += 1
        return [u for u in (res or []) if isinstance(u, dict)]

    @staticmethod
    def _name(frm: dict[str, Any] | None) -> str:
        frm = frm or {}
        return str(frm.get("first_name") or frm.get("username") or frm.get("id") or "Ryan")

    async def handle_update(self, u: dict[str, Any], on_callback: OnCallback, on_command: OnCommand) -> None:
        """One update → the right handler. Taps and commands from any other chat are ignored."""
        self.updates_seen += 1
        cq = u.get("callback_query")
        if isinstance(cq, dict):
            msg = cq.get("message") or {}
            chat = str(((msg.get("chat") or {}).get("id")) or ((cq.get("from") or {}).get("id")) or "")
            if chat != self.chat_id:
                log.warning("callback from a foreign chat ignored")
                return
            data = str(cq.get("data") or "")
            answer = await on_callback(data, self._name(cq.get("from")), str(cq.get("id")), msg.get("message_id"))
            await self.answer_callback(str(cq.get("id")), answer)
            return
        m = u.get("message")
        if isinstance(m, dict) and m.get("text"):
            chat = str((m.get("chat") or {}).get("id") or "")
            if chat != self.chat_id:
                return
            text = str(m["text"]).strip()
            if not text.startswith("/"):
                return
            parts = text.split(None, 1)
            cmd = parts[0].lower().split("@")[0]
            reply = await on_command(cmd, parts[1] if len(parts) > 1 else "", self._name(m.get("from")))
            if reply:
                try:
                    await self.send(reply)
                except Exception as e:  # noqa: BLE001
                    log.warning("reply to %s failed: %s", cmd, e)

    async def poll(self, stop: asyncio.Event, *, on_callback: OnCallback, on_command: OnCommand,
                   load_offset: Callable[[], Awaitable[int]] | None = None, save_offset: Callable[[int], Awaitable[None]] | None = None) -> None:
        """Long-poll until `stop` is set. Transport errors back off; a 409 conflict backs off longer (the other consumer)."""
        offset = 0
        if load_offset is not None:
            try:
                offset = int(await load_offset() or 0)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram offset unavailable (%s); starting at 0", e)
        backoff = 2.0
        while not stop.is_set():
            try:
                updates = await self.get_updates(offset)
                backoff = 2.0
            except TelegramConflict as e:
                self.errors += 1
                self.last_error = f"409 conflict: {e}"
                log.warning("getUpdates conflict (%s); another consumer is polling — retrying in 10 s", e)
                await self.clock.sleep(10.0)
                continue
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                log.warning("getUpdates failed: %s; retrying in %.0fs", self.last_error, backoff)
                await self.clock.sleep(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            if not updates:
                continue
            for u in updates:
                uid = int(u.get("update_id") or 0)
                offset = max(offset, uid + 1)
                self.last_update_id = uid
                try:
                    await self.handle_update(u, on_callback, on_command)
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    self.last_error = f"handler: {type(e).__name__}: {str(e)[:160]}"
                    log.exception("update handler failed")
            if save_offset is not None:
                try:
                    await save_offset(offset)
                except Exception as e:  # noqa: BLE001
                    log.debug("could not persist the telegram offset: %s", e)

    def status(self) -> dict[str, Any]:
        return {"configured": self.configured, "sent": self.sent, "edited": self.edited, "polls": self.polls, "updates": self.updates_seen,
                "errors": self.errors, "last_error": self.last_error}


__all__ = ["TelegramBot", "TelegramConflict"]
