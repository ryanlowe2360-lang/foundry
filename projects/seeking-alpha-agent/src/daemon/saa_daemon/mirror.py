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
        """Both Supabase key styles work: a legacy `service_role` JWT (eyJ…) goes on `apikey` + `Authorization: Bearer`;
        a new secret key (`sb_secret_…`) is not a JWT and must go on `apikey` only (a Bearer copy would fail JWT checks)."""
        key = self.settings.supabase_key.value
        h = {"apikey": key, "Accept": "application/json", "Prefer": "return=representation"}
        if not key.startswith("sb_"):
            h["Authorization"] = f"Bearer {key}"
        return h

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

    async def flush_engine_trades(self, limit: int = 200) -> int:
        """Shadow-ledger rows → saa.shadow_trades (source 'engine', keyed by engine_key)."""
        if not self.enabled:
            return 0
        rows = self.store.dirty_engine_trades(limit)
        if not rows:
            return 0
        await self.rpc("saa_engine_shadow_upsert", {"p_rows": [r["payload"] for r in rows]})
        for r in rows:
            self.store.mark_engine_trade_mirrored(r["engine_key"], r["updated_at"])
        self.rows_pushed["engine_trades"] = self.rows_pushed.get("engine_trades", 0) + len(rows)
        return len(rows)

    async def flush_engine_decisions(self, limit: int = 500) -> int:
        if not self.enabled:
            return 0
        rows = self.store.dirty_engine_decisions(limit)
        if not rows:
            return 0
        await self.rpc("saa_engine_decisions_insert", {"p_rows": [{"trade_date": r["trade_date"], "run_id": r["run_id"], **r["payload"]} for r in rows]})
        self.store.mark_engine_decisions_mirrored([r["id"] for r in rows])
        self.rows_pushed["engine_decisions"] = self.rows_pushed.get("engine_decisions", 0) + len(rows)
        return len(rows)

    # ------------------------------------------------------------ execution (M4)
    async def _flush_keyed(self, name: str, rpc: str, dirty: Any, mark: Any, key: str, limit: int) -> int:
        if not self.enabled:
            return 0
        rows = dirty(limit)
        if not rows:
            return 0
        await self.rpc(rpc, {"p_rows": [{"trade_date": r["trade_date"], "run_id": r["run_id"], **r["payload"]} for r in rows]})
        for r in rows:
            mark(r[key], r["updated_at"])
        self.rows_pushed[name] = self.rows_pushed.get(name, 0) + len(rows)
        return len(rows)

    async def flush_paper_orders(self, limit: int = 200) -> int:
        return await self._flush_keyed("paper_orders", "saa_paper_orders_upsert", self.store.dirty_paper_orders, self.store.mark_paper_order_mirrored, "ticket_id", limit)

    async def flush_paper_trades(self, limit: int = 200) -> int:
        return await self._flush_keyed("paper_trades", "saa_paper_trades_upsert", self.store.dirty_paper_trades, self.store.mark_paper_trade_mirrored, "engine_key", limit)

    async def flush_approvals(self, limit: int = 200) -> int:
        return await self._flush_keyed("approvals", "saa_approvals_upsert", self.store.dirty_approvals, self.store.mark_approval_mirrored, "proposal_id", limit)

    async def flush_reconciliations(self, limit: int = 200) -> int:
        if not self.enabled:
            return 0
        rows = self.store.dirty_reconciliations(limit)
        if not rows:
            return 0
        await self.rpc("saa_reconciliations_insert", {"p_rows": [{"ts": r["ts"], "trade_date": r["trade_date"], "run_id": r["run_id"], "ok": bool(r["ok"]),
                                                                    **{k: v for k, v in r["payload"].items() if k != "ts"}} for r in rows]})
        self.store.mark_reconciliations_mirrored([r["id"] for r in rows])
        self.rows_pushed["reconciliations"] = self.rows_pushed.get("reconciliations", 0) + len(rows)
        return len(rows)

    async def flush_all(self) -> dict[str, int]:
        """One mirror round. Each part is independent; failures are counted, never raised."""
        out: dict[str, int] = {}
        for name, fn in (("queue", self.flush_queue), ("bars", self.flush_bars), ("snapshots", self.flush_snapshots),
                         ("vix", self.flush_vix), ("halts", self.flush_halts), ("engine_trades", self.flush_engine_trades),
                         ("engine_decisions", self.flush_engine_decisions), ("paper_orders", self.flush_paper_orders),
                         ("paper_trades", self.flush_paper_trades), ("approvals", self.flush_approvals), ("reconciliations", self.flush_reconciliations)):
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

    async def rules_latest(self) -> dict[str, Any] | None:
        data = await self.rpc("saa_rules_latest", {})
        return data if isinstance(data, dict) and data.get("params") else None

    async def checklists_today(self, d: str | None = None) -> list[dict[str, Any]]:
        data = await self.rpc("saa_checklists_today", {"p_date": d} if d else {})
        return [r for r in (data or []) if isinstance(r, dict)]

    async def engine_ledger(self, limit: int = 60) -> list[dict[str, Any]]:
        """Closed gate-fired engine trades, oldest first: [{engine_key, trade_date, r_result, counts_for_rails}]."""
        data = await self.rpc("saa_engine_ledger", {"p_limit": limit})
        return [r for r in (data or []) if isinstance(r, dict)]

    async def set_setting(self, key: str, value: str) -> None:
        await self.rpc("saa_set_setting", {"p_key": key, "p_value": value})

    async def calendar_day(self, d: str | None = None) -> dict[str, Any]:
        data = await self.rpc("saa_calendar_day", {"p_date": d} if d else {})
        return data if isinstance(data, dict) else {}

    async def get_setting(self, key: str) -> str | None:
        data = await self.rpc("saa_get_setting", {"p_key": key})
        return None if data in (None, "") else str(data)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "calls": self.calls, "failures": self.failures, "last_error": self.last_error,
                "queue": self.store.queue_size() if self.enabled else 0, "pushed": dict(self.rows_pushed)}
