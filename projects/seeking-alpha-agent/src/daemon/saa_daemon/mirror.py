"""Supabase mirror: PostgREST RPC calls to the `public.saa_*` surface (service role), and the flush
loop that pushes SQLite's dirty rows (bars, chain snapshots, VIX, halts) plus the durable RPC queue
(outbox messages, run_log entries) into Postgres. Idempotent upserts make retries safe."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .config import Settings
from .http import HttpClient
from .store import BarRow, Store

log = logging.getLogger("saa.mirror")


class MirrorError(Exception):
    pass


class SupabaseMirror:
    def __init__(self, settings: Settings, store: Store, http: HttpClient | None):
        self.settings = settings
        self.store = store
        self.http = http
        self.enabled = settings.mirror_enabled and settings.mirror_configured and http is not None
        self.calls = 0
        self.failures = 0
        self.last_error: str | None = None
        self.last_ok: datetime | None = None
        self.rows_pushed: dict[str, int] = {"bars": 0, "snapshots": 0, "vix": 0, "halts": 0, "queue": 0}
        if not self.enabled:
            log.warning("Supabase mirror DISABLED (%s) — data stays in SQLite only",
                        "SAA_MIRROR=false" if not settings.mirror_enabled else "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

    # ---------------------------------------------------------------- transport
    def _headers(self) -> dict[str, str]:
        key = self.settings.supabase_key.value
        return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json", "Prefer": "return=representation"}

    async def rpc(self, fn: str, args: dict[str, Any] | None = None, *, timeout: float = 20.0) -> Any:
        """Call public.<fn>(args). Raises MirrorError on any failure (message never contains the key)."""
        if not self.enabled or self.http is None:
            raise MirrorError("mirror disabled")
        url = f"{self.settings.supabase_url}/rest/v1/rpc/{fn}"
        self.calls += 1
        try:
            status, data = await self.http.post_json(url, args or {}, headers=self._headers(), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            self.last_error = f"{fn}: {type(e).__name__}: {str(e)[:160]}"
            raise MirrorError(self.last_error) from e
        if status >= 400:
            self.failures += 1
            msg = data.get("message") if isinstance(data, dict) else str(data)
            self.last_error = f"{fn}: HTTP {status}: {str(msg)[:160]}"
            raise MirrorError(self.last_error)
        self.last_ok = datetime.now().astimezone()
        return data

    # ------------------------------------------------------------ durable queue
    def queue(self, fn: str, args: dict[str, Any], now: datetime) -> None:
        """Persist an RPC call to retry until it succeeds (outbox, run_log, run row patches)."""
        if not self.enabled:
            return
        self.store.enqueue_rpc(fn, args, now)

    async def flush_queue(self, limit: int = 50) -> int:
        if not self.enabled:
            return 0
        n = 0
        for item in self.store.queued_rpcs(limit):
            try:
                await self.rpc(item["rpc"], item["args"])
            except MirrorError as e:
                self.store.fail_rpc(item["id"], str(e))
                log.warning("mirror queue failed: %s (queued %d)", e, self.store.queue_size())
                self.rows_pushed["queue"] += n
                return -1 if n == 0 else n  # keep order; the rest is retried next round
            self.store.dequeue_rpc(item["id"])
            n += 1
        self.rows_pushed["queue"] += n
        return n

    # ------------------------------------------------------------- dirty rows
    async def flush_bars(self, limit: int = 1500) -> int:
        if not self.enabled:
            return 0
        rows: list[BarRow] = self.store.dirty_bars(limit)
        if not rows:
            return 0
        await self.rpc("saa_bars_upsert", {"p_rows": [r.payload() for r in rows]})
        self.store.mark_bars_mirrored(rows)
        self.rows_pushed["bars"] += len(rows)
        return len(rows)

    async def flush_snapshots(self, limit: int = 50) -> int:
        if not self.enabled:
            return 0
        n = 0
        for s in self.store.dirty_snapshots(limit):
            await self.rpc("saa_chain_snapshot", {
                "p_ts": s["ts"], "p_underlying": s["underlying"], "p_spot": s["spot"],
                "p_expirations": json.loads(s["expirations"]), "p_summary": json.loads(s["summary"]),
                "p_gamma": json.loads(s["gamma"]) if s["gamma"] else None, "p_source": "dxlink"})
            self.store.mark_snapshot_mirrored(s["id"])
            n += 1
        self.rows_pushed["snapshots"] += n
        return n

    async def flush_vix(self) -> int:
        if not self.enabled:
            return 0
        n = 0
        for v in self.store.dirty_vix():
            await self.rpc("saa_snapshot", {"p_kind": "vix_term", "p_payload": {
                "ts": v["ts"], "vix": v["vix"], "vix1d": v["vix1d"], "vix9d": v["vix9d"], "vix3m": v["vix3m"],
                "source": v["source"] or "cboe", "errors": json.loads(v["errors"] or "[]"), "by": "daemon"}})
            self.store.mark_vix_mirrored(v["ts"])
            n += 1
        self.rows_pushed["vix"] += n
        return n

    async def flush_halts(self) -> int:
        if not self.enabled:
            return 0
        rows = self.store.dirty_halts()
        if not rows:
            return 0
        payload = [{"symbol": h["symbol"], "halt_time": h["halt_time"], "reason_code": h["reason_code"], "market": h["market"],
                    "resumption_time": h["resumption_time"], "source": h["source"],
                    "raw": json.loads(h["raw"]) if h["raw"] else None} for h in rows]
        await self.rpc("saa_halts_upsert", {"p_rows": payload})
        for h in rows:
            self.store.mark_halt_mirrored(h["symbol"], h["halt_time"])
        self.rows_pushed["halts"] += len(rows)
        return len(rows)

    async def flush_all(self) -> dict[str, int]:
        """One mirror round. Each part is independent; failures are counted, never raised."""
        out: dict[str, int] = {}
        for name, fn in (("queue", self.flush_queue), ("bars", self.flush_bars), ("snapshots", self.flush_snapshots),
                         ("vix", self.flush_vix), ("halts", self.flush_halts)):
            try:
                out[name] = await fn()
            except MirrorError as e:
                out[name] = -1
                log.warning("mirror %s failed: %s", name, e)
        return out

    # --------------------------------------------------------------- reads
    async def active_symbols(self) -> list[str]:
        data = await self.rpc("saa_active_symbols", {})
        return [str(s).upper() for s in (data or []) if s]

    async def calendar_day(self, d: str | None = None) -> dict[str, Any]:
        data = await self.rpc("saa_calendar_day", {"p_date": d} if d else {})
        return data if isinstance(data, dict) else {}

    async def get_setting(self, key: str) -> str | None:
        data = await self.rpc("saa_get_setting", {"p_key": key})
        return None if data in (None, "") else str(data)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "calls": self.calls, "failures": self.failures, "last_error": self.last_error,
                "queue": self.store.queue_size() if self.enabled else 0, "pushed": dict(self.rows_pushed)}
