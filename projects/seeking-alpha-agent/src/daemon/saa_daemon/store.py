"""SQLite hot state. One file per daemon install (`state/saa.sqlite`), WAL mode.

Everything the daemon knows is written here first; the Supabase mirror reads the `mirrored = 0`
rows and flips them once Postgres has them, so a network outage never loses data.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
create table if not exists bars_1m (
  symbol text not null, bar_time text not null,
  open real, high real, low real, close real, volume real, vwap real, trade_count integer,
  complete integer not null default 0, updated_at text not null, mirrored integer not null default 0,
  primary key (symbol, bar_time));
create index if not exists bars_dirty on bars_1m (mirrored);
create table if not exists latest_quotes (
  symbol text primary key, ts text, bid real, ask real, bid_size real, ask_size real,
  last real, day_volume real, day_open real, prev_close real, trading_status text);
create table if not exists chain_snapshots (
  id integer primary key autoincrement, ts text not null, underlying text not null, spot real,
  expirations text not null, summary text not null, gamma text, mirrored integer not null default 0);
create index if not exists snaps_dirty on chain_snapshots (mirrored);
create index if not exists snaps_und on chain_snapshots (underlying, ts);
create table if not exists vix_term (
  ts text primary key, vix real, vix1d real, vix9d real, vix3m real, source text, errors text, mirrored integer not null default 0);
create table if not exists halts (
  symbol text not null, halt_time text not null, reason_code text, market text, resumption_time text,
  source text not null, raw text, mirrored integer not null default 0, primary key (symbol, halt_time));
create table if not exists mirror_queue (
  id integer primary key autoincrement, rpc text not null, args text not null, created_at text not null,
  attempts integer not null default 0, last_error text);
create table if not exists runs (
  run_id text primary key, trade_date text not null, started_at text not null, ended_at text, status text not null,
  stats text not null default '{}');
create table if not exists kv (key text primary key, value text not null);
create table if not exists events_log (
  id integer primary key autoincrement, ts text not null, level text not null, task text not null, message text not null);
create table if not exists engine_trades (
  engine_key text primary key, trade_date text not null, run_id text not null, symbol text not null, source text not null,
  status text not null, r_result real, payload text not null, updated_at text not null, mirrored integer not null default 0);
create index if not exists engine_trades_dirty on engine_trades (mirrored);
create table if not exists engine_decisions (
  id integer primary key autoincrement, ts text not null, trade_date text not null, run_id text not null, symbol text not null,
  kind text not null, decision text not null, reason text not null, payload text not null, mirrored integer not null default 0);
create index if not exists engine_decisions_dirty on engine_decisions (mirrored);
create table if not exists paper_orders (
  ticket_id text primary key, engine_key text not null, trade_date text not null, run_id text not null, symbol text not null,
  status text not null, payload text not null, updated_at text not null, mirrored integer not null default 0);
create index if not exists paper_orders_dirty on paper_orders (mirrored);
create table if not exists paper_trades (
  engine_key text primary key, trade_date text not null, run_id text not null, symbol text not null, status text not null,
  realized_r real, payload text not null, updated_at text not null, mirrored integer not null default 0);
create index if not exists paper_trades_dirty on paper_trades (mirrored);
create table if not exists approvals (
  proposal_id text primary key, engine_key text not null, trade_date text not null, run_id text not null, status text not null,
  payload text not null, updated_at text not null, mirrored integer not null default 0);
create index if not exists approvals_dirty on approvals (mirrored);
create table if not exists reconciliations (
  id integer primary key autoincrement, ts text not null, trade_date text not null, run_id text not null, ok integer not null,
  payload text not null, mirrored integer not null default 0);
create index if not exists reconciliations_dirty on reconciliations (mirrored);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class BarRow:
    symbol: str
    bar_time: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None
    trade_count: int | None
    complete: bool
    updated_at: str | None = None   # set when read back from SQLite; used to mark exactly that version as mirrored

    def payload(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "bar_time": iso(self.bar_time), "open": self.open, "high": self.high, "low": self.low,
                "close": self.close, "volume": self.volume, "vwap": self.vwap, "trade_count": self.trade_count,
                "complete": self.complete}


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if str(self.path) != ":memory:":
            self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ bars
    def upsert_bars(self, rows: Iterable[BarRow], now: datetime) -> int:
        n = 0
        with self.db:
            for r in rows:
                self.db.execute(
                    """insert into bars_1m (symbol, bar_time, open, high, low, close, volume, vwap, trade_count, complete, updated_at, mirrored)
                       values (?,?,?,?,?,?,?,?,?,?,?,0)
                       on conflict(symbol, bar_time) do update set open=excluded.open, high=excluded.high, low=excluded.low,
                         close=excluded.close, volume=excluded.volume, vwap=excluded.vwap, trade_count=excluded.trade_count,
                         complete=max(bars_1m.complete, excluded.complete), updated_at=excluded.updated_at, mirrored=0""",
                    (r.symbol, iso(r.bar_time), r.open, r.high, r.low, r.close, r.volume, r.vwap, r.trade_count,
                     1 if r.complete else 0, iso(now)))
                n += 1
        return n

    def dirty_bars(self, limit: int = 2000) -> list[BarRow]:
        cur = self.db.execute("select * from bars_1m where mirrored = 0 order by bar_time limit ?", (limit,))
        return [BarRow(r["symbol"], from_iso(r["bar_time"]), r["open"], r["high"], r["low"], r["close"], r["volume"], r["vwap"],
                       r["trade_count"], bool(r["complete"]), r["updated_at"]) for r in cur]

    def mark_bars_mirrored(self, rows: Iterable[BarRow]) -> None:
        """Flip mirrored=1 only for the exact version that was sent (a newer update keeps the row dirty)."""
        with self.db:
            for r in rows:
                if r.updated_at is None:
                    self.db.execute("update bars_1m set mirrored = 1 where symbol = ? and bar_time = ?", (r.symbol, iso(r.bar_time)))
                else:
                    self.db.execute("update bars_1m set mirrored = 1 where symbol = ? and bar_time = ? and updated_at = ?",
                                    (r.symbol, iso(r.bar_time), r.updated_at))

    def bar_stats(self, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
        """Complete-bar count and the missing minutes inside [start, end)."""
        cur = self.db.execute("select bar_time, complete from bars_1m where symbol = ? and bar_time >= ? and bar_time < ? order by bar_time",
                              (symbol, iso(start), iso(end)))
        rows = cur.fetchall()
        have = {from_iso(r["bar_time"]) for r in rows}
        complete = sum(1 for r in rows if r["complete"])
        expected = int((end - start).total_seconds() // 60)
        # explicit minute walk (clear and cheap: 390 iterations)
        missing: list[datetime] = []
        t = start
        while t < end:
            if t not in have:
                missing.append(t)
            t += timedelta(minutes=1)
        return {"bars": len(rows), "complete": complete, "expected": expected, "missing": len(missing),
                "first_gap": iso(missing[0]) if missing else None,
                "first": iso(min(have)) if have else None, "last": iso(max(have)) if have else None}

    # ---------------------------------------------------------------- quotes
    def upsert_quote(self, symbol: str, ts: datetime, **fields: Any) -> None:
        cols = ["symbol", "ts"] + list(fields)
        vals = [symbol, iso(ts)] + list(fields.values())
        sets = ", ".join(f"{c}=excluded.{c}" for c in cols[1:])
        with self.db:
            self.db.execute(f"insert into latest_quotes ({', '.join(cols)}) values ({', '.join('?' * len(cols))}) "
                            f"on conflict(symbol) do update set {sets}", vals)

    def latest_quote(self, symbol: str) -> dict[str, Any] | None:
        r = self.db.execute("select * from latest_quotes where symbol = ?", (symbol,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------- snapshots
    def insert_snapshot(self, ts: datetime, underlying: str, spot: float | None, expirations: list[dict[str, Any]],
                        summary: dict[str, Any], gamma: dict[str, Any] | None) -> int:
        with self.db:
            cur = self.db.execute("insert into chain_snapshots (ts, underlying, spot, expirations, summary, gamma, mirrored) values (?,?,?,?,?,?,0)",
                                  (iso(ts), underlying, spot, json.dumps(expirations, separators=(",", ":")),
                                   json.dumps(summary, separators=(",", ":")), json.dumps(gamma, separators=(",", ":")) if gamma is not None else None))
            return int(cur.lastrowid)

    def dirty_snapshots(self, limit: int = 200) -> list[dict[str, Any]]:
        cur = self.db.execute("select * from chain_snapshots where mirrored = 0 order by id limit ?", (limit,))
        return [dict(r) for r in cur]

    def mark_snapshot_mirrored(self, snap_id: int) -> None:
        with self.db:
            self.db.execute("update chain_snapshots set mirrored = 1 where id = ?", (snap_id,))

    def snapshot_counts(self, start: datetime, end: datetime) -> dict[str, int]:
        cur = self.db.execute("select underlying, count(*) n from chain_snapshots where ts >= ? and ts <= ? group by underlying",
                              (iso(start), iso(end)))
        return {r["underlying"]: r["n"] for r in cur}

    def latest_snapshot(self, underlying: str) -> dict[str, Any] | None:
        r = self.db.execute("select * from chain_snapshots where underlying = ? order by id desc limit 1", (underlying,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["expirations"] = json.loads(d["expirations"])
        d["summary"] = json.loads(d["summary"])
        d["gamma"] = json.loads(d["gamma"]) if d["gamma"] else None
        return d

    # ------------------------------------------------------------------- vix
    def insert_vix(self, ts: datetime, term: dict[str, Any], source: str = "cboe") -> None:
        with self.db:
            self.db.execute("insert or replace into vix_term (ts, vix, vix1d, vix9d, vix3m, source, errors, mirrored) values (?,?,?,?,?,?,?,0)",
                            (iso(ts), term.get("vix"), term.get("vix1d"), term.get("vix9d"), term.get("vix3m"), source,
                             json.dumps(term.get("errors", []))))

    def dirty_vix(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("select * from vix_term where mirrored = 0 order by ts")]

    def mark_vix_mirrored(self, ts: str) -> None:
        with self.db:
            self.db.execute("update vix_term set mirrored = 1 where ts = ?", (ts,))

    def vix_range(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("select * from vix_term where ts >= ? and ts <= ? order by ts", (iso(start), iso(end)))]

    # ----------------------------------------------------------------- halts
    def upsert_halts(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.db:
            for h in rows:
                cur = self.db.execute(
                    """insert into halts (symbol, halt_time, reason_code, market, resumption_time, source, raw, mirrored)
                       values (?,?,?,?,?,?,?,0)
                       on conflict(symbol, halt_time) do update set
                         reason_code = coalesce(excluded.reason_code, halts.reason_code),
                         resumption_time = coalesce(excluded.resumption_time, halts.resumption_time),
                         raw = coalesce(excluded.raw, halts.raw),
                         mirrored = case when coalesce(excluded.resumption_time,'') <> coalesce(halts.resumption_time,'') then 0 else halts.mirrored end""",
                    (h["symbol"], h["halt_time"], h.get("reason_code"), h.get("market"), h.get("resumption_time"),
                     h.get("source", "nasdaq_rss"), json.dumps(h.get("raw")) if h.get("raw") is not None else None))
                n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return n

    def dirty_halts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("select * from halts where mirrored = 0 order by halt_time")]

    def mark_halt_mirrored(self, symbol: str, halt_time: str) -> None:
        with self.db:
            self.db.execute("update halts set mirrored = 1 where symbol = ? and halt_time = ?", (symbol, halt_time))

    def halts_since(self, start: datetime) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("select * from halts where halt_time >= ? order by halt_time", (iso(start),))]

    # ------------------------------------------------------------ mirror queue
    def enqueue_rpc(self, rpc: str, args: dict[str, Any], now: datetime) -> int:
        with self.db:
            cur = self.db.execute("insert into mirror_queue (rpc, args, created_at) values (?,?,?)",
                                  (rpc, json.dumps(args, separators=(",", ":"), default=str), iso(now)))
            return int(cur.lastrowid)

    def queued_rpcs(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.db.execute("select * from mirror_queue order by id limit ?", (limit,))]
        for r in rows:
            r["args"] = json.loads(r["args"])
        return rows

    def queue_size(self) -> int:
        return int(self.db.execute("select count(*) from mirror_queue").fetchone()[0])

    def dequeue_rpc(self, qid: int) -> None:
        with self.db:
            self.db.execute("delete from mirror_queue where id = ?", (qid,))

    def fail_rpc(self, qid: int, error: str) -> None:
        with self.db:
            self.db.execute("update mirror_queue set attempts = attempts + 1, last_error = ? where id = ?", (error[:300], qid))

    # ------------------------------------------------------------------ runs
    def upsert_run(self, run_id: str, trade_date: str, started_at: datetime, status: str, stats: dict[str, Any],
                   ended_at: datetime | None = None) -> None:
        with self.db:
            self.db.execute("""insert into runs (run_id, trade_date, started_at, ended_at, status, stats) values (?,?,?,?,?,?)
                               on conflict(run_id) do update set ended_at=excluded.ended_at, status=excluded.status, stats=excluded.stats""",
                            (run_id, trade_date, iso(started_at), iso(ended_at) if ended_at else None, status,
                             json.dumps(stats, separators=(",", ":"), default=str)))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        r = self.db.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["stats"] = json.loads(d["stats"])
        return d

    # ---------------------------------------------------------------- engine
    def upsert_engine_trade(self, row: dict[str, Any], run_id: str, now: datetime) -> None:
        """One shadow-ledger row (Position.row()); re-upserted on every change, mirrored flag reset each time."""
        with self.db:
            self.db.execute(
                """insert into engine_trades (engine_key, trade_date, run_id, symbol, source, status, r_result, payload, updated_at, mirrored)
                   values (?,?,?,?,?,?,?,?,?,0)
                   on conflict(engine_key) do update set status=excluded.status, r_result=excluded.r_result, payload=excluded.payload,
                     updated_at=excluded.updated_at, mirrored=0""",
                (row["engine_key"], row["trade_date"], run_id, row["symbol"], row["engine_source"], row["status"], row.get("r_result"),
                 json.dumps(row, separators=(",", ":"), default=str), iso(now)))

    def dirty_engine_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.db.execute("select * from engine_trades where mirrored = 0 order by updated_at limit ?", (limit,))]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def mark_engine_trade_mirrored(self, engine_key: str, updated_at: str) -> None:
        with self.db:
            self.db.execute("update engine_trades set mirrored = 1 where engine_key = ? and updated_at = ?", (engine_key, updated_at))

    def engine_trades(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        q = "select * from engine_trades" + (" where trade_date = ?" if trade_date else "") + " order by engine_key"
        rows = [dict(r) for r in self.db.execute(q, (trade_date,) if trade_date else ())]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def insert_engine_decisions(self, rows: Iterable[dict[str, Any]], trade_date: str, run_id: str) -> int:
        n = 0
        with self.db:
            for d in rows:
                self.db.execute("insert into engine_decisions (ts, trade_date, run_id, symbol, kind, decision, reason, payload, mirrored) values (?,?,?,?,?,?,?,?,0)",
                                (d["ts"], trade_date, run_id, d["symbol"], d["kind"], d["decision"], d["reason"][:400], json.dumps(d, separators=(",", ":"), default=str)))
                n += 1
        return n

    def dirty_engine_decisions(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.db.execute("select * from engine_decisions where mirrored = 0 order by id limit ?", (limit,))]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def mark_engine_decisions_mirrored(self, ids: Iterable[int]) -> None:
        with self.db:
            for i in ids:
                self.db.execute("update engine_decisions set mirrored = 1 where id = ?", (i,))

    # ------------------------------------------------------------- execution (M4)
    def _upsert_keyed(self, table: str, key_col: str, key: str, trade_date: str, run_id: str, symbol: str | None, status: str,
                      payload: dict[str, Any], now: datetime, extra: dict[str, Any] | None = None) -> None:
        cols = [key_col, "trade_date", "run_id"] + (["symbol"] if symbol is not None else []) + ["status"] + list(extra or {}) + ["payload", "updated_at", "mirrored"]
        vals = [key, trade_date, run_id] + ([symbol] if symbol is not None else []) + [status] + list((extra or {}).values()) + \
               [json.dumps(payload, separators=(",", ":"), default=str), iso(now), 0]
        sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key_col)
        with self.db:
            self.db.execute(f"insert into {table} ({', '.join(cols)}) values ({', '.join('?' * len(cols))}) on conflict({key_col}) do update set {sets}", vals)

    def _rows(self, table: str, where: str, args: tuple[Any, ...], order: str, limit: int | None = None) -> list[dict[str, Any]]:
        q = f"select * from {table}" + (f" where {where}" if where else "") + f" order by {order}" + (f" limit {int(limit)}" if limit else "")
        rows = [dict(r) for r in self.db.execute(q, args)]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def _mark(self, table: str, key_col: str, key: str, updated_at: str) -> None:
        with self.db:
            self.db.execute(f"update {table} set mirrored = 1 where {key_col} = ? and updated_at = ?", (key, updated_at))

    def upsert_paper_order(self, row: dict[str, Any], trade_date: str, run_id: str, now: datetime) -> None:
        self._upsert_keyed("paper_orders", "ticket_id", row["ticket_id"], trade_date, run_id, row["symbol"], row["status"], row, now,
                           extra={"engine_key": row["engine_key"]})

    def paper_orders(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        return self._rows("paper_orders", "trade_date = ?" if trade_date else "", (trade_date,) if trade_date else (), "ticket_id")

    def dirty_paper_orders(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("paper_orders", "mirrored = 0", (), "updated_at", limit)

    def mark_paper_order_mirrored(self, ticket_id: str, updated_at: str) -> None:
        self._mark("paper_orders", "ticket_id", ticket_id, updated_at)

    def upsert_paper_trade(self, row: dict[str, Any], trade_date: str, run_id: str, now: datetime) -> None:
        self._upsert_keyed("paper_trades", "engine_key", row["engine_key"], trade_date, run_id, row["symbol"], row["status"], row, now,
                           extra={"realized_r": row.get("realized_r")})

    def paper_trades(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        return self._rows("paper_trades", "trade_date = ?" if trade_date else "", (trade_date,) if trade_date else (), "engine_key")

    def open_paper_trades(self) -> list[dict[str, Any]]:
        return self._rows("paper_trades", "status in ('working','open','closing','partial')", (), "engine_key")

    def dirty_paper_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("paper_trades", "mirrored = 0", (), "updated_at", limit)

    def mark_paper_trade_mirrored(self, engine_key: str, updated_at: str) -> None:
        self._mark("paper_trades", "engine_key", engine_key, updated_at)

    def upsert_approval(self, row: dict[str, Any], trade_date: str, run_id: str, now: datetime) -> None:
        self._upsert_keyed("approvals", "proposal_id", row["proposal_id"], trade_date, run_id, None, row["status"], {**row, "engine_key": row["engine_key"]}, now,
                           extra={"engine_key": row["engine_key"]})

    def approvals(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        return self._rows("approvals", "trade_date = ?" if trade_date else "", (trade_date,) if trade_date else (), "proposal_id")

    def dirty_approvals(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("approvals", "mirrored = 0", (), "updated_at", limit)

    def mark_approval_mirrored(self, proposal_id: str, updated_at: str) -> None:
        self._mark("approvals", "proposal_id", proposal_id, updated_at)

    def insert_reconciliation(self, ts: datetime, trade_date: str, run_id: str, ok: bool, payload: dict[str, Any]) -> int:
        with self.db:
            cur = self.db.execute("insert into reconciliations (ts, trade_date, run_id, ok, payload, mirrored) values (?,?,?,?,?,0)",
                                  (iso(ts), trade_date, run_id, 1 if ok else 0, json.dumps(payload, separators=(",", ":"), default=str)))
            return int(cur.lastrowid)

    def reconciliations(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        return self._rows("reconciliations", "trade_date = ?" if trade_date else "", (trade_date,) if trade_date else (), "id")

    def dirty_reconciliations(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("reconciliations", "mirrored = 0", (), "id", limit)

    def mark_reconciliations_mirrored(self, ids: Iterable[int]) -> None:
        with self.db:
            for i in ids:
                self.db.execute("update reconciliations set mirrored = 1 where id = ?", (i,))

    # -------------------------------------------------------------------- kv
    def set_kv(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("insert into kv (key, value) values (?,?) on conflict(key) do update set value=excluded.value", (key, value))

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        r = self.db.execute("select value from kv where key = ?", (key,)).fetchone()
        return r["value"] if r else default

    # ------------------------------------------------------------ events log
    def log_event(self, ts: datetime, level: str, task: str, message: str) -> None:
        with self.db:
            self.db.execute("insert into events_log (ts, level, task, message) values (?,?,?,?)", (iso(ts), level, task, message[:2000]))

    def error_events(self, since: datetime) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("select * from events_log where level in ('ERROR','CRITICAL') and ts >= ? order by id", (iso(since),))]
