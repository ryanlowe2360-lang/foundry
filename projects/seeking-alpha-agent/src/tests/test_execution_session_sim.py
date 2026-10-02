"""M4 through the real daemon loop on the virtual clock (the M3 simulation plus a FakeBroker on the sandbox seat and a
scripted Telegram): the engine fires SPY at 09:37, the daemon proposes it with Approve / Skip buttons, "Ryan" taps
Approve 9 s later, the limit-at-mid ladder fills in the fake sandbox, the engine's close drives the exit, the round trip
is reconciled, and everything lands in SQLite + the saa_* RPCs + the Telegram heartbeat / EOD.

Three days: a normal day (round trip), a `/halt` day (flat within 10 s, entries blocked, `/resume`), and a delayed-feed
day (the engine never fires, so the executor never proposes — and the order path would refuse anyway, D19)."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from saa_daemon import config
from saa_daemon.clock import FakeClock, Schedule, et, et_dt
from saa_daemon.daemon import Daemon
from saa_daemon.engine.replay import replay_engine
from saa_daemon.execution import FakeBroker, occ_to_streamer
from saa_daemon.mirror import SupabaseMirror
from saa_daemon.store import Store
from saa_daemon.telegram import Notifier
from test_daemon_session_sim import FakeBrokerage
from test_engine_session_sim import EngineFeed, EngineHttp

D = date(2026, 9, 28)
SCHED = Schedule.for_date(D)


class M4Http(EngineHttp):
    """EngineHttp + a scripted Telegram Bot API: proposals get an Approve tap `approve_after_s` later; `commands` are
    (time, text) messages from Ryan; getUpdates long-polls on the virtual clock like the real thing."""

    def __init__(self, fixtures: Path, clock: FakeClock, *, approve_after_s: float = 9.0, commands: list[tuple[datetime, str]] | None = None):
        super().__init__(fixtures)
        self.clock = clock
        self.approve_after_s = approve_after_s
        self.tg_calls: list[tuple[str, dict]] = []
        self.pending: list[dict] = []
        self.next_update = 100
        self.msg_id = 700
        for when, text in commands or []:
            self.pending.append({"due": when, "update_id": None, "message": {"message_id": 1, "chat": {"id": 987654321, "type": "private"},
                                                                           "from": {"first_name": "Ryan"}, "text": text}})

    async def post_json(self, url, body, *, headers=None, timeout=15.0):
        if "api.telegram.org" not in url:
            return await super().post_json(url, body, headers=headers, timeout=timeout)
        method = url.rsplit("/", 1)[-1]
        self.tg_calls.append((method, body))
        now = self.clock.now()
        if method == "sendMessage":
            self.msg_id += 1
            kb = (body.get("reply_markup") or {}).get("inline_keyboard")
            if kb and self.approve_after_s is not None:
                self.pending.append({"due": now + timedelta(seconds=self.approve_after_s), "update_id": None,
                                     "callback_query": {"id": f"cb{self.msg_id}", "from": {"id": 987654321, "first_name": "Ryan"}, "data": kb[0][0]["callback_data"],
                                                        "message": {"message_id": self.msg_id, "chat": {"id": 987654321}}}})
            return 200, {"ok": True, "result": {"message_id": self.msg_id}}
        if method in ("editMessageText", "answerCallbackQuery"):
            return 200, {"ok": True, "result": True}
        if method == "getUpdates":
            offset = int(body["offset"])
            deadline = self.clock.now() + timedelta(seconds=float(body.get("timeout", 20)))
            while True:   # a real long poll returns the moment an update arrives, else at the timeout
                for u in sorted((u for u in self.pending if u["update_id"] is None and u["due"] <= self.clock.now()), key=lambda u: u["due"]):
                    self.next_update += 1          # ids are assigned on arrival, like Telegram does
                    u["update_id"] = self.next_update
                ready = sorted((u for u in self.pending if u["update_id"] is not None and u["update_id"] >= offset), key=lambda u: u["update_id"])
                if ready:
                    return 200, {"ok": True, "result": [{k: v for k, v in u.items() if k != "due"} for u in ready]}
                if self.clock.now() >= deadline:
                    return 200, {"ok": True, "result": []}
                await self.clock.sleep(1.0)
        return 404, {"ok": False, "description": "unknown"}


async def _run(tmp_path: Path, fixtures: Path, env_file: Path, *, lag_ms: int = 0, commands: list[tuple[datetime, str]] | None = None,
               approve_after_s: float | None = 9.0):
    settings = config.load_settings(env_file, environ={"SAA_MAX_SINGLE_NAMES": "2"}, state_dir=tmp_path / "state")
    clock = FakeClock(et_dt(D, time(9, 15)))
    store = Store(tmp_path / "state" / "saa.sqlite")
    http = M4Http(fixtures, clock, approve_after_s=approve_after_s, commands=commands)
    mirror = SupabaseMirror(settings, store, http)
    notifier = Notifier(settings, mirror, http)
    brokerage = FakeBrokerage()
    feed = EngineFeed(clock, drop_at=et_dt(D, time(11, 0)), lag_ms=lag_ms)
    holder: dict = {}

    def quote(occ: str):
        d = holder.get("daemon")
        if d is None:
            return None
        return d._option_quote(occ_to_streamer(occ))

    broker = FakeBroker(mode="at_limit", account_masked="…9103", quote_fn=quote)
    broker.now_fn = clock.now

    async def broker_factory(_b):
        return broker

    daemon = Daemon(settings, clock=clock, store=store, http=http, brokerage=brokerage, feed_factory=lambda _b: feed, mirror=mirror, notifier=notifier,
                    mode="session", trade_date=D, host="testmac", broker_factory=broker_factory)
    holder["daemon"] = daemon
    task = asyncio.create_task(daemon.run_session())
    deadline = SCHED.shutdown + timedelta(minutes=10)
    for _ in range(300):
        await clock.run_until(deadline)
        if task.done():
            break
        await asyncio.sleep(0)
    assert task.done(), "daemon did not finish on the virtual clock"
    return daemon, task.result(), http, store, broker


@pytest.mark.asyncio
async def test_m4_round_trip_through_the_daemon(env_file: Path, tmp_path: Path, fixtures: Path):
    daemon, res, http, store, broker = await _run(tmp_path, fixtures, env_file)
    assert res.unhandled == 0 and res.ok and res.errors == {"feed": 1}
    ex = daemon.executor
    assert ex is not None and daemon.execution_off_reason is None and daemon.bot is not None and daemon.bot.configured
    s = res.stats["execution"]
    assert s["mode"] == "approval" and s["account_masked"] == "…9103" and s["require_realtime"] and not s["halted"]
    # --- the SPY gate fire became a proposal, an approval 9 s later, a fill at the mid, and an engine-driven exit
    spy = [t for t in ex.trades.values() if t.symbol == "SPY"]
    assert len(spy) == 1
    t = spy[0]
    assert t.status == "closed" and t.decision == "approved" and t.decision_latency_s == pytest.approx(9.0, abs=1.5)
    assert t.entry_qty == 1 and t.entry_price is not None and t.entry_at is not None
    assert t.entry_price <= t.shadow_entry_ask + 0.05 and t.entry_price >= t.shadow_entry_bid - 0.05
    assert t.exit_qty == 1 and t.exit_reason in ("trail", "time_stop") and t.realized_r is not None and t.shadow_r is not None
    assert abs(t.realized_r - t.shadow_r) < 0.5                     # same marks, a few cents of slippage at most
    assert t.slippage_entry is not None and t.slippage_exit is not None and t.exits[-1]["kind"] == "exit"
    assert et(t.exit_at).time() <= time(10, 0, 30)
    assert s["counts"]["proposed"] == 1 and s["counts"]["approved"] == 1 and s["counts"]["filled"] == 1 and s["closed"] == 1 and s["open"] == 0
    assert s["counts"]["blocked"] == 0 and s["counts"]["refused"] == 0 and s["counts"]["timeout"] == 0
    assert await broker.positions() == [] and broker.cash == pytest.approx(1000.0 + t.realized_pnl, abs=0.01)
    # --- Telegram: proposal with buttons, approve tap answered, message edited; outbox carries the paper alerts
    sends = [b for m, b in http.tg_calls if m == "sendMessage"]
    assert sends[0]["text"].startswith("Proposed ▸ SPY long call .SPY260928C") and sends[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Approve"
    assert "⏱ Approve within 3:00 or it is skipped." in sends[0]["text"]
    assert [b["text"] for m, b in http.tg_calls if m == "answerCallbackQuery"] == ["Approved ✅ placing the paper order"]
    assert any("Approved by Ryan" in b["text"] for m, b in http.tg_calls if m == "editMessageText")
    polls = [b for m, b in http.tg_calls if m == "getUpdates"]
    assert polls and polls[0]["offset"] == 0 and polls[0]["allowed_updates"] == ["message", "callback_query"]
    enq = [b for fn, b in http.calls if fn == "saa_enqueue"]
    alerts = [b["p_text"] for b in enq if b["p_kind"] == "alert"]
    assert any(a.startswith("PAPER open ▸ SPY .SPY260928C") for a in alerts) and any(a.startswith("PAPER close ▸ SPY .SPY260928C") for a in alerts)
    hb, eod = [b["p_text"] for b in enq if b["p_kind"] == "system"][:2]
    assert "Paper: sandbox …9103 · approval mode via Telegram buttons (3-min timeout = Skip) · kill switch armed (/halt) · gated on real-time feed" in hb
    assert re.search(r"Paper: 1 proposed · ✅1 ⏭0 ⏱0 ✖0 · 1 filled · 0 unfilled · 0 rejected · 0 blocked · 0 refused · 1 closed → [+-]\d+\.\d\dR", eod)
    assert "reconcile ok" in eod
    # --- persistence + mirror: paper tables flushed through the RPC surface, nothing left dirty, offsets persisted
    calls: dict[str, list[dict]] = {}
    for fn, body in http.calls:
        calls.setdefault(fn, []).append(body)
    trades = {r["engine_key"]: r for b in calls["saa_paper_trades_upsert"] for r in b["p_rows"]}
    assert trades[t.engine_key]["status"] == "closed" and trades[t.engine_key]["realized_r"] == t.realized_r and trades[t.engine_key]["run_id"] == daemon.run_id
    orders = {r["ticket_id"]: r for b in calls["saa_paper_orders_upsert"] for r in b["p_rows"]}
    assert sorted(r["kind"] for r in orders.values()) == ["entry", "exit"] and all(r["status"] == "filled" for r in orders.values())
    appr = [r for b in calls["saa_approvals_upsert"] for r in b["p_rows"]]
    assert appr[-1]["status"] == "approved" and appr[-1]["latency_s"] == pytest.approx(9.0, abs=1.5)
    recs = [r for b in calls["saa_reconciliations_insert"] for r in b["p_rows"]]
    assert len(recs) >= 700 and all(r["ok"] for r in recs) and s["counts"]["reconciliations"] == len(recs)
    offs = [b for b in calls["saa_set_setting"] if b["p_key"] == "telegram_update_offset"]
    assert offs and int(offs[-1]["p_value"]) == 102
    assert [b for b in calls["saa_get_setting"] if b["p_key"] == "halt"]
    assert store.dirty_paper_trades() == [] and store.dirty_paper_orders() == [] and store.dirty_approvals() == [] and store.dirty_reconciliations() == []
    assert store.paper_trades(D.isoformat())[0]["status"] == "closed" and len(store.paper_orders(D.isoformat())) == 2
    session_log = calls["saa_log_run"][-1]
    assert session_log["p_job"] == "daemon:session" and session_log["p_detail"]["execution"]["closed"] == 1
    for sec in config.load_settings(env_file, environ={}, state_dir=tmp_path).secret_values():
        assert sec not in str(http.calls) and sec not in str(http.tg_calls)
    # --- the engine is untouched by execution: its recording still replays byte for byte
    rec = Path(res.stats["recording"])
    live = json.loads(daemon.engine.canonical())
    rep = json.loads(replay_engine(rec).canonical)
    assert rep["decisions"] == live["decisions"] and rep["ledger"] == live["ledger"] and rep["state"] == live["state"]


@pytest.mark.asyncio
async def test_m4_halt_flattens_within_ten_seconds_and_blocks_entries(env_file: Path, tmp_path: Path, fixtures: Path):
    daemon, res, http, store, broker = await _run(tmp_path, fixtures, env_file, commands=[(et_dt(D, time(9, 40)), "/halt"), (et_dt(D, time(10, 30)), "/resume")])
    assert res.unhandled == 0 and res.ok
    ex = daemon.executor
    s = res.stats["execution"]
    assert ex is not None and len(s["halt_events"]) == 1
    h = s["halt_events"][0]
    assert h["by"] == "Ryan" and h["reason"] == "/halt" and h["ok"] and h["seconds"] <= 10 and h["within_budget"] and h["closed"]
    t = [x for x in ex.trades.values() if x.symbol == "SPY"][0]
    assert t.status == "closed" and t.exit_reason == "halt by Ryan" and t.exits[-1]["kind"] == "flatten" and et(t.exit_at).time() <= time(9, 40, 40)
    assert not daemon.killswitch.engaged                          # /resume at 10:30 cleared it
    assert not s["halted"] and s["counts"]["halts"] == 1
    enq = [b for fn, b in http.calls if fn == "saa_enqueue"]
    sys_msgs = [b["p_text"] for b in enq if b["p_kind"] == "system"]
    assert any(m.startswith("HALT ▸ by Ryan (/halt): flat in") and "1 position(s) closed" in m for m in sys_msgs)
    assert any(m.startswith("RESUME ▸ kill switch cleared by Ryan") for m in sys_msgs)
    halt_settings = [b["p_value"] for fn, b in http.calls if fn == "saa_set_setting" and b["p_key"] == "halt"]
    assert halt_settings == ["true", "false"]
    eod = sys_msgs[-1]
    assert "HALT by Ryan flat in" in eod
    assert (tmp_path / "state" / "HALT").exists() is False
    assert await broker.positions() == []


@pytest.mark.asyncio
async def test_m4_delayed_feed_places_nothing(env_file: Path, tmp_path: Path, fixtures: Path):
    daemon, res, http, store, broker = await _run(tmp_path, fixtures, env_file, lag_ms=900_000)
    assert res.unhandled == 0 and res.ok
    s = res.stats["execution"]
    assert s["feed_mode"] in ("DELAYED", None) and s["trades"] == 0 and s["counts"]["proposed"] == 0 and s["counts"]["events"] == 0
    assert not [m for m, b in http.tg_calls if m == "sendMessage"] and not [c for c in broker.calls if c[0] == "place"]
    assert store.paper_trades(D.isoformat()) == []
