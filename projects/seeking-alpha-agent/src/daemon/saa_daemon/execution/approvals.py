"""Approval mode (M4–M5): every gate-fired *paper* entry is proposed to Ryan on Telegram — "Proposed … Approve / Skip" —
and placed only after Approve. No answer within 3 minutes = **Skip**, logged as such (`saa.approvals.status = timeout`).

The gate owns the pending proposals and their futures; the Telegram poller resolves them (`resolve`), the executor
awaits them (`wait`) and expires them when the engine closes the shadow position before a decision arrives. Exits
never need approval — getting out is risk reduction. All waiting goes through the injected clock (virtual in tests).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from ..clock import Clock, et
from ..store import Store

log = logging.getLogger("saa.approvals")

APPROVE, SKIP = "ok", "skip"


class Messenger(Protocol):
    """A Telegram-shaped transport: a message with inline buttons, editable afterwards."""

    async def send(self, text: str, *, buttons: list[tuple[str, str]] | None = None) -> int | None: ...
    async def edit(self, message_id: int, text: str) -> None: ...


@dataclass
class Proposal:
    proposal_id: str
    engine_key: str
    symbol: str
    text: str
    created_at: datetime
    timeout_s: float
    status: str = "pending"            # pending | approved | skipped | timeout | expired | failed
    message_id: int | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    latency_s: float | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def row(self) -> dict[str, Any]:
        return {"proposal_id": self.proposal_id, "engine_key": self.engine_key, "symbol": self.symbol, "text": self.text,
                "sent_at": self.created_at.isoformat(), "timeout_s": self.timeout_s, "status": self.status, "message_id": self.message_id,
                "decided_at": self.decided_at.isoformat() if self.decided_at else None, "decided_by": self.decided_by,
                "latency_s": self.latency_s, "note": self.note, **self.extra}


def proposal_id_for(engine_key: str, now: datetime) -> str:
    return hashlib.sha1(f"{engine_key}|{now.isoformat()}".encode("utf-8")).hexdigest()[:10]


class ApprovalGate:
    def __init__(self, messenger: Messenger | None, clock: Clock, store: Store | None = None, *, timeout_s: float = 180.0,
                 run_id: str = "", trade_date: date | None = None):
        self.messenger = messenger
        self.clock = clock
        self.store = store
        self.timeout_s = float(timeout_s)
        self.run_id = run_id
        self.trade_date = trade_date
        self.pending: dict[str, tuple[Proposal, asyncio.Future]] = {}
        self.history: list[Proposal] = []
        self.counts: dict[str, int] = {"proposed": 0, "approved": 0, "skipped": 0, "timeout": 0, "expired": 0, "failed": 0}

    # ------------------------------------------------------------------------------------------- persistence
    def _persist(self, p: Proposal) -> None:
        if self.store is None:
            return
        td = (self.trade_date or et(p.created_at).date()).isoformat()
        try:
            self.store.upsert_approval(p.row(), td, self.run_id, self.clock.now())
        except Exception as e:  # noqa: BLE001
            log.error("approval persist failed for %s: %s", p.proposal_id, e)

    @staticmethod
    def _fmt(seconds: float) -> str:
        m, s = divmod(int(round(seconds)), 60)
        return f"{m}:{s:02d}"

    # ---------------------------------------------------------------------------------------------- propose
    async def propose(self, engine_key: str, symbol: str, text: str, **extra: Any) -> Proposal:
        now = self.clock.now()
        p = Proposal(proposal_id_for(engine_key, now), engine_key, symbol, text, now, self.timeout_s, extra=dict(extra))
        self.counts["proposed"] += 1
        self.history.append(p)
        if self.messenger is None:
            p.status, p.note, p.decided_at = "failed", "no approval channel (Telegram bot not configured)", now
            self.counts["failed"] += 1
            self._persist(p)
            return p
        full = f"{text}\n⏱ Approve within {self._fmt(self.timeout_s)} or it is skipped."
        try:
            p.message_id = await self.messenger.send(full, buttons=[("✅ Approve", f"appr:{p.proposal_id}:{APPROVE}"), ("⏭ Skip", f"appr:{p.proposal_id}:{SKIP}")])
        except Exception as e:  # noqa: BLE001
            p.status, p.note, p.decided_at = "failed", f"proposal could not be sent: {type(e).__name__}: {str(e)[:120]}", now
            self.counts["failed"] += 1
            self._persist(p)
            log.error("proposal %s not sent: %s", p.proposal_id, p.note)
            return p
        loop = asyncio.get_running_loop()
        self.pending[p.proposal_id] = (p, loop.create_future())
        self._persist(p)
        log.info("proposed %s for %s (message %s)", p.proposal_id, engine_key, p.message_id)
        return p

    # ------------------------------------------------------------------------------------------------- wait
    async def wait(self, p: Proposal) -> str:
        """Block until Ryan decides, the position expires, or the timeout passes. Returns the final status."""
        if p.status != "pending":
            return p.status
        entry = self.pending.get(p.proposal_id)
        if entry is None:
            return p.status
        fut = entry[1]
        sleeper = asyncio.create_task(self.clock.sleep(self.timeout_s))
        try:
            done, _ = await asyncio.wait({fut, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not sleeper.done():
                sleeper.cancel()
        if fut in done:
            return p.status
        # timeout → Skip
        self._settle(p, "timeout", "timeout", f"no answer in {self._fmt(self.timeout_s)} → skipped")
        await self._edit(p, f"⏱ No answer in {self._fmt(self.timeout_s)} — SKIPPED (timeout).")
        return p.status

    def _settle(self, p: Proposal, status: str, by: str, note: str) -> None:
        now = self.clock.now()
        p.status, p.decided_by, p.decided_at, p.note = status, by, now, note
        p.latency_s = round((now - p.created_at).total_seconds(), 1)
        self.counts[status if status in self.counts else "failed"] = self.counts.get(status, 0) + 1
        entry = self.pending.pop(p.proposal_id, None)
        if entry is not None and not entry[1].done():
            entry[1].set_result(status)
        self._persist(p)
        log.info("proposal %s %s by %s after %ss", p.proposal_id, status, by, p.latency_s)

    async def _edit(self, p: Proposal, suffix: str) -> None:
        if self.messenger is None or p.message_id is None:
            return
        try:
            await self.messenger.edit(p.message_id, f"{p.text}\n{suffix}")
        except Exception as e:  # noqa: BLE001
            log.debug("could not edit proposal message %s: %s", p.message_id, e)

    # ---------------------------------------------------------------------------------------------- resolve
    async def resolve(self, proposal_id: str, decision: str, by: str) -> Proposal | None:
        """Called by the Telegram poller for a button tap. Returns the proposal, or None for an unknown/late tap."""
        entry = self.pending.get(proposal_id)
        if entry is None:
            return None
        p = entry[0]
        if decision == APPROVE:
            self._settle(p, "approved", by, "approved")
            await self._edit(p, f"✅ Approved by {by} at {et(p.decided_at):%H:%M:%S} ({self._fmt(p.latency_s or 0)}).")
        else:
            self._settle(p, "skipped", by, "skipped by Ryan")
            await self._edit(p, f"⏭ Skipped by {by} at {et(p.decided_at):%H:%M:%S}.")
        return p

    async def expire(self, engine_key: str, reason: str) -> list[Proposal]:
        """The engine closed the shadow position (or the day ended) before a decision: nothing to approve any more."""
        out = []
        for pid, (p, _fut) in list(self.pending.items()):
            if p.engine_key == engine_key or engine_key == "*":
                self._settle(p, "expired", "engine", reason)
                await self._edit(p, f"✖ Expired — {reason}.")
                out.append(p)
        return out

    def pending_for(self, engine_key: str) -> Proposal | None:
        for p, _ in self.pending.values():
            if p.engine_key == engine_key:
                return p
        return None

    def summary(self) -> dict[str, Any]:
        return {**self.counts, "pending": len(self.pending), "timeout_s": self.timeout_s}


__all__ = ["ApprovalGate", "Proposal", "Messenger", "APPROVE", "SKIP", "proposal_id_for"]
