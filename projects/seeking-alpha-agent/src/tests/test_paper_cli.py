"""The paper self-test flows (`saa_daemon.paper`) offline: a round trip through the real ladder on the FakeBroker,
the halt test through the real `Executor.halt`, the approval test with a fake Telegram bot, the status text, and the
`halt` / `resume` CLI flag commands."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path

import pytest

from saa_daemon import config
from saa_daemon.clock import FakeClock, et_dt
from saa_daemon.execution import FakeBroker, KillSwitch, LadderPolicy
from saa_daemon.paper import approval_test, halt_test, paper_status_text, roundtrip
from saa_daemon.paper_cli import run_halt_flag, run_paper_status
from saa_daemon.store import Store

D = date(2026, 9, 28)
SYM, OCC = ".SPY260928C654", "SPY   260928C00654000"


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return et_dt(D, time(hh, mm, ss))


async def _drive(clock: FakeClock, task: asyncio.Task, until: datetime) -> None:
    for _ in range(500):
        await clock.run_until(until)
        if task.done():
            return
        await asyncio.sleep(0)
    assert task.done()


@pytest.mark.asyncio
async def test_roundtrip_fills_both_legs_and_reconciles(tmp_path: Path):
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="at_limit", account_masked="…9103")
    b.now_fn = clock.now
    b.quotes[OCC] = (1.12, 1.16)
    store = Store(tmp_path / "saa.sqlite")
    rt = await roundtrip(b, store, clock, SYM, (1.12, 1.16), run_id="rt-test", trade_date=D)
    assert rt.ok and rt.reconciled and rt.entry["status"] == "filled" and rt.exit["status"] == "filled"
    assert rt.entry["avg_fill_price"] == 1.14 and rt.exit["avg_fill_price"] == 1.14         # at_limit: mid both ways
    assert rt.realized_pnl == 0.0 and rt.realized_r == 0.0 and rt.symbol == "SPY" and rt.occ == OCC
    assert await b.positions() == [] and [c[0] for c in b.calls if c[0] == "place"] == ["place", "place"]
    rows = store.paper_trades(D.isoformat())
    assert len(rows) == 1 and rows[0]["status"] == "closed" and rows[0]["payload"]["lane"] == "roundtrip" and rows[0]["payload"]["engine_key"].startswith("roundtrip|")
    assert sorted(o["payload"]["kind"] for o in store.paper_orders(D.isoformat())) == ["entry", "exit"]


@pytest.mark.asyncio
async def test_roundtrip_reports_an_unfilled_entry(tmp_path: Path):
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="never")
    b.now_fn = clock.now
    b.quotes[OCC] = (1.12, 1.16)
    store = Store(tmp_path / "saa.sqlite")
    task = asyncio.create_task(roundtrip(b, store, clock, SYM, (1.12, 1.16), run_id="rt-test", trade_date=D,
                                         ladder=LadderPolicy(step_seconds=5, steps=3, fill_wait_seconds=5)))
    await _drive(clock, task, at(10, 2))
    rt = task.result()
    assert not rt.ok and rt.entry["status"] == "unfilled" and rt.exit["status"] == "skipped" and rt.reconciled
    assert rt.realized_pnl is None and store.paper_trades(D.isoformat())[0]["status"] == "unfilled"


@pytest.mark.asyncio
async def test_roundtrip_exit_escalates_to_flatten(tmp_path: Path):
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="at_limit")
    b.now_fn = clock.now
    b.quotes[OCC] = (1.12, 1.16)
    store = Store(tmp_path / "saa.sqlite")

    # entry fills at the limit; then the book stops filling limits → the exit ladder fails and the flatten (market) finishes it
    placed = {"n": 0}
    orig_place = b.place

    async def place(symbol, action, quantity, price, *, external_id=None):
        placed["n"] += 1
        if placed["n"] == 2:
            b.mode = "never"
        return await orig_place(symbol, action, quantity, price, external_id=external_id)

    b.place = place  # type: ignore[method-assign]
    task = asyncio.create_task(roundtrip(b, store, clock, SYM, (1.12, 1.16), run_id="rt-test", trade_date=D,
                                         ladder=LadderPolicy(step_seconds=5, steps=3, fill_wait_seconds=5)))
    await _drive(clock, task, at(10, 3))
    rt = task.result()
    assert rt.ok and rt.exit["status"] == "filled" and rt.exit["filled_quantity"] == 1 and rt.exit["avg_fill_price"] == 1.12   # market at the bid
    assert await b.positions() == []


@pytest.mark.asyncio
async def test_halt_test_flattens_within_budget_and_rearms(tmp_path: Path):
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="at_limit")
    b.now_fn = clock.now
    b.quotes[OCC] = (1.12, 1.16)
    store = Store(tmp_path / "saa.sqlite")
    notes = []

    async def notify(kind, text):
        notes.append((kind, text))

    orig_place = b.place

    async def place(symbol, action, quantity, price, *, external_id=None):
        o = await orig_place(symbol, action, quantity, price, external_id=external_id)
        b.mode = "never"              # after the entry, limits stop filling: the flatten must reach market
        return o

    b.place = place  # type: ignore[method-assign]
    task = asyncio.create_task(halt_test(b, store, clock, SYM, (1.12, 1.16), run_id="ht-test", trade_date=D, state_dir=tmp_path / "state", notify=notify))
    await _drive(clock, task, at(10, 2))
    out = task.result()
    assert out["ok"] and out["within_10s"] and out["seconds"] <= 10 and out["position_before"] == [("SPY   260928C00654000", 1)] and out["position_after"] == []
    assert out["halt"]["closed"] and out["trade"]["status"] == "closed" and out["trade"]["exit_reason"] == "halt by cli"
    assert out.get("kill_switch_cleared") and not KillSwitch(tmp_path / "state").engaged
    assert any(n[1].startswith("HALT ▸ by cli") for n in notes)


class FakeBot:
    """A TelegramBot stand-in: records sends/edits; `script` decides the tap (None = never → timeout)."""

    def __init__(self, clock: FakeClock, script: str | None, after_s: float = 5.0):
        self.clock, self.script, self.after_s = clock, script, after_s
        self.sent: list[dict] = []
        self.edits: list[str] = []
        self.configured = True

    async def send(self, text, *, buttons=None):
        self.sent.append({"text": text, "buttons": buttons})
        return 900

    async def edit(self, message_id, text):
        self.edits.append(text)

    async def poll(self, stop, *, on_callback, on_command, load_offset=None, save_offset=None):
        if load_offset is not None:
            await load_offset()
        if self.script is None:
            await stop.wait()
            return
        await self.clock.sleep(self.after_s)
        if not stop.is_set():
            data = self.sent[-1]["buttons"][0 if self.script == "ok" else 1][1]
            await on_callback(data, "Ryan", "cb1", 900)
        if save_offset is not None:
            await save_offset(42)
        await stop.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("script,expected", [("ok", "approved"), ("skip", "skipped"), (None, "timeout")])
async def test_approval_test_records_the_outcome(tmp_path: Path, script, expected):
    clock = FakeClock(at(10, 0))
    store = Store(tmp_path / "saa.sqlite")
    bot = FakeBot(clock, script)
    pings = {"n": 0}

    async def keepalive():
        pings["n"] += 1

    task = asyncio.create_task(approval_test(bot, store, clock, timeout_s=30.0, run_id="at-test", trade_date=D, keepalive=keepalive))
    await _drive(clock, task, at(10, 1))
    out = task.result()
    assert out["ok"] and out["status"] == expected and out["proposal_id"]
    assert bot.sent[0]["text"].startswith("Proposed ▸ TEST approval flow (no order)") and [b[0] for b in bot.sent[0]["buttons"]] == ["✅ Approve", "⏭ Skip"]
    if expected == "timeout":
        assert out["logged_as"] == "skip (timeout)" and out["latency_s"] == 30.0 and any("SKIPPED (timeout)" in e for e in bot.edits)
    else:
        assert out["latency_s"] == 5.0 and out["decided_by"] == "Ryan"
    assert store.approvals(D.isoformat())[0]["status"] == expected and pings["n"] >= 1


@pytest.mark.asyncio
async def test_halt_and_resume_commands_and_status_text(tmp_path: Path, capsys):
    p = tmp_path / ".env"
    p.write_text("TT_PROD_CLIENT_ID=x\nSAA_MIRROR=false\n", encoding="utf-8")
    settings = config.load_settings(p, environ={}, state_dir=tmp_path / "state")
    assert await run_halt_flag(settings, engage=True, reason="test") == 0
    k = KillSwitch(tmp_path / "state")
    assert k.engaged and k.info()["reason"] == "test" and Store(tmp_path / "state" / "saa.sqlite").get_kv("halt") == "true"
    assert run_paper_status(settings) == 0
    out = capsys.readouterr().out
    assert "kill switch: ENGAGED" in out and "Paper book" in out
    assert await run_halt_flag(settings, engage=False, reason="") == 0
    assert not k.engaged and Store(tmp_path / "state" / "saa.sqlite").get_kv("halt") == "false"
    txt = paper_status_text(Store(tmp_path / "state" / "saa.sqlite"), D)
    assert txt.startswith(f"Paper book {D}: 0 trade(s)") and "kill switch: armed" in txt
