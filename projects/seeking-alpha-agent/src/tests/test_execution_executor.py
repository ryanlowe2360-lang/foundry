"""The executor on the virtual clock with the FakeBroker and a fake Telegram messenger: approval flow (approve / skip /
3-minute timeout = Skip / expired), the D19 feed gate on the order path, Tier 1 order caps refused at the order, the
exit and bank paths with realized R and slippage, the kill switch (flat within 10 s, unknown positions included) and
the 30-second reconciliation."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from saa_daemon.clock import FakeClock, et_dt
from saa_daemon.execution import ApprovalGate, ExecutionPolicy, Executor, FakeBroker, KillSwitch, LadderPolicy
from saa_daemon.execution.executor import ALLOW_AUTO_MODE
from saa_daemon.store import Store

D = date(2026, 9, 28)
OCC = "SPY   260928C00654000"
SYM = ".SPY260928C654"


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return et_dt(D, time(hh, mm, ss))


class FakeMessenger:
    def __init__(self, fail: bool = False):
        self.sent: list[dict[str, Any]] = []
        self.edits: list[tuple[int, str]] = []
        self.fail = fail
        self._ids = 100

    async def send(self, text: str, *, buttons=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self._ids += 1
        self.sent.append({"id": self._ids, "text": text, "buttons": buttons})
        return self._ids

    async def edit(self, message_id: int, text: str) -> None:
        self.edits.append((message_id, text))


class Harness:
    def __init__(self, tmp_path: Path, *, broker: FakeBroker | None = None, messenger: FakeMessenger | None = None, account: float = 1000.0,
                 require_realtime: bool = True, mode: str = "approval", timeout_s: float = 180.0):
        self.clock = FakeClock(at(9, 37, 2))
        self.broker = broker or FakeBroker(mode="at_limit")
        self.broker.now_fn = self.clock.now
        self.broker.quotes[OCC] = (1.12, 1.16)
        self.store = Store(tmp_path / "saa.sqlite")
        self.messenger = messenger if messenger is not None else FakeMessenger()
        self.approvals = ApprovalGate(self.messenger, self.clock, self.store, timeout_s=timeout_s, run_id="run-1", trade_date=D)
        self.kill = KillSwitch(tmp_path / "state")
        self.notes: list[tuple[str, str]] = []
        self.errors: list[tuple[str, BaseException]] = []
        self.halt_setting: list[bool] = []

        async def notify(kind: str, text: str) -> None:
            self.notes.append((kind, text))

        policy = ExecutionPolicy(mode=mode, require_realtime=require_realtime, approval_timeout_s=timeout_s,
                                 ladder=LadderPolicy(step_seconds=5.0, steps=3, fill_wait_seconds=5.0, poll_seconds=1.0))
        self.ex = Executor(broker=self.broker, store=self.store, clock=self.clock, approvals=self.approvals, killswitch=self.kill, policy=policy,
                           account=account, run_id="run-1", trade_date=D, quote_fn=self.quote, notify=notify,
                           on_error=lambda task, e: self.errors.append((task, e)), set_halt_setting=self.halt_setting.append)

    def quote(self, symbol: str):
        from saa_daemon.execution import streamer_to_occ
        return self.broker.quotes.get(streamer_to_occ(symbol))

    async def run_until(self, when: datetime, *, settle: int = 20) -> None:
        for _ in range(settle):
            await asyncio.sleep(0)
        await self.clock.run_until(when)
        for _ in range(settle):
            await asyncio.sleep(0)

    async def settle(self, n: int = 30) -> None:
        for _ in range(n):
            await asyncio.sleep(0)


def open_event(key: str = "2026-09-28|SPY|gate|open|long|0937", *, contracts: int = 1, ask: float = 1.16, bid: float = 1.12, symbol: str = "SPY",
               option_symbol: str = SYM, direction: str = "long", source: str = "gate", mode: str = "floor") -> dict[str, Any]:
    return {"type": "open", "at": at(9, 37, 2).isoformat(), "position": key, "symbol": symbol, "source": source, "lane": "open",
            "row": {"engine_key": key, "symbol": symbol, "option_symbol": option_symbol, "direction": direction, "option_type": "call" if direction == "long" else "put",
                    "window": "open", "lane": "open", "contracts": contracts, "entry_bid": bid, "entry_premium": ask, "probability": 0.32,
                    "sizing": {"mode": mode, "risk_dollars": contracts * ask * 100}, "gates": {"passed": 6}, "window_end": at(10, 0).isoformat()}}


def close_event(key: str = "2026-09-28|SPY|gate|open|long|0937", *, reason: str = "trail", exit_bid: float = 1.60, r: float = 0.379) -> dict[str, Any]:
    return {"type": "close", "at": at(9, 50, 2).isoformat(), "position": key, "symbol": "SPY", "source": "gate", "lane": "open", "r_result": r,
            "reason": reason, "row": {"exit_premium": exit_bid, "r_result": r}}


# ------------------------------------------------------------------------------------------------- approval flow
@pytest.mark.asyncio
async def test_approve_places_the_entry_and_the_close_event_exits(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event()], feed_mode="realtime", lag_s=1.0)
    await h.settle()
    t = h.ex.trades["2026-09-28|SPY|gate|open|long|0937"]
    assert t.status == "proposed" and t.proposal_id and len(h.messenger.sent) == 1
    msg = h.messenger.sent[0]
    assert msg["text"].startswith("Proposed ▸ SPY long call .SPY260928C654 ×1 @ ask 1.16 (mid 1.14) · R $116 (floor) · open · 6/6 gates · p=0.32 · stop 10:00")
    assert "Approve within 3:00 or it is skipped" in msg["text"]
    assert [b[0] for b in msg["buttons"]] == ["✅ Approve", "⏭ Skip"] and msg["buttons"][0][1] == f"appr:{t.proposal_id}:ok"
    # Ryan taps Approve 9 s later
    await h.run_until(at(9, 37, 11))
    p = await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    assert p is not None and p.status == "approved" and p.latency_s == 9.0
    await h.settle()
    assert t.status == "open" and t.entry_qty == 1 and t.entry_price == 1.14 and t.decision == "approved" and t.decision_latency_s == 9.0
    assert t.slippage_entry == pytest.approx(-0.02)                 # filled at mid 1.14 vs the engine's ask 1.16
    assert h.ex.counts["approved"] == 1 and h.ex.counts["filled"] == 1
    assert any("Approved by Ryan at 09:37:11" in e[1] for e in h.messenger.edits)
    assert h.notes[-1][0] == "alert" and h.notes[-1][1].startswith("PAPER open ▸ SPY .SPY260928C654 ×1 filled @ 1.14")
    pos = await h.broker.positions()
    assert len(pos) == 1 and pos[0].symbol == OCC and pos[0].quantity == 1
    # the engine closes on the trail at 09:50 with the bid at 1.60
    h.broker.quotes[OCC] = (1.60, 1.64)
    await h.ex.on_engine_events([close_event()], feed_mode="realtime")
    await h.run_until(at(9, 50, 30))
    assert t.status == "closed" and t.exit_qty == 1 and t.exit_price == 1.62 and t.exit_reason == "trail"
    assert t.realized_pnl == pytest.approx(48.0) and t.realized_r == pytest.approx(48 / 114, abs=1e-4)
    assert t.slippage_exit == pytest.approx(0.02) and t.shadow_r == 0.379
    assert await h.broker.positions() == []
    assert h.notes[-1][1].startswith("PAPER close ▸ SPY .SPY260928C654 ×1 @ 1.62 → +0.42R ($+48) · trail · shadow +0.38R")
    # persisted: trade + approval + two tickets
    rows = h.store.paper_trades(D.isoformat())
    assert len(rows) == 1 and rows[0]["status"] == "closed" and rows[0]["realized_r"] == pytest.approx(48 / 114, abs=1e-4)
    assert [a["status"] for a in h.store.approvals(D.isoformat())] == ["approved"]
    assert sorted(o["payload"]["kind"] for o in h.store.paper_orders(D.isoformat())) == ["entry", "exit"]
    assert h.errors == []


@pytest.mark.asyncio
async def test_skip_places_nothing(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    await h.approvals.resolve(t.proposal_id, "skip", "Ryan")
    await h.settle()
    assert t.status == "skipped" and t.entry is None and not [c for c in h.broker.calls if c[0] == "place"]
    assert h.ex.counts["skipped"] == 1 and h.store.approvals(D.isoformat())[0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_timeout_is_logged_as_skip(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    await h.run_until(at(9, 40, 1))                    # 2:59 — still pending
    assert t.status == "proposed" and h.approvals.pending
    await h.run_until(at(9, 40, 3))                    # 3:01 — timed out
    assert t.status == "timeout" and t.decision == "timeout" and t.decision_latency_s == 180.0 and not h.approvals.pending
    assert not [c for c in h.broker.calls if c[0] == "place"]
    a = h.store.approvals(D.isoformat())[0]
    assert a["status"] == "timeout" and a["payload"]["note"] == "no answer in 3:00 → skipped" and a["payload"]["latency_s"] == 180.0
    assert any("No answer in 3:00 — SKIPPED (timeout)" in e[1] for e in h.messenger.edits)
    assert h.ex.counts["timeout"] == 1
    # a late tap is ignored
    assert await h.approvals.resolve(t.proposal_id, "ok", "Ryan") is None


@pytest.mark.asyncio
async def test_engine_close_before_decision_expires_the_proposal(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    await h.ex.on_engine_events([close_event(reason="time_stop")], feed_mode="realtime")
    await h.settle()
    assert t.status == "expired" and "time_stop" in t.notes[-1] and not [c for c in h.broker.calls if c[0] == "place"]
    assert any("Expired — position closed by the engine (time_stop) before a decision" in e[1] for e in h.messenger.edits)


@pytest.mark.asyncio
async def test_no_messenger_means_the_proposal_fails_closed(tmp_path: Path):
    h = Harness(tmp_path, messenger=FakeMessenger(fail=True))
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    assert t.status == "failed" and "could not be sent" in t.notes[-1] and not [c for c in h.broker.calls if c[0] == "place"]


# --------------------------------------------------------------------------------------------------- the gates
@pytest.mark.asyncio
async def test_delayed_feed_blocks_the_order_path_even_if_the_engine_fired(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event()], feed_mode="DELAYED", lag_s=900.0)
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    assert t.status == "blocked" and t.block_reason.startswith("feed_not_realtime: mode DELAYED, lag 900.0s")
    assert h.messenger.sent == [] and not [c for c in h.broker.calls if c[0] == "place"] and h.ex.counts["blocked"] == 1
    assert h.store.paper_trades(D.isoformat())[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_fast_lane_opens_never_trade(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event("2026-09-28|SPY|fast_lane|orb|long|0937", source="fast_lane")], feed_mode="realtime")
    await h.settle()
    assert h.ex.trades == {} and h.messenger.sent == []


@pytest.mark.asyncio
async def test_tier1_order_caps_are_refused_at_the_order(tmp_path: Path):
    h = Harness(tmp_path, account=1000.0)
    await h.ex.on_engine_events([open_event("k-too-many", contracts=3)], feed_mode="realtime")          # > 2 per $1k
    await h.ex.on_engine_events([open_event("k-too-rich", contracts=1, ask=6.00, bid=5.90)], feed_mode="realtime")   # $600 > 50 %
    await h.settle()
    a, b = h.ex.trades["k-too-many"], h.ex.trades["k-too-rich"]
    assert a.status == "refused" and "3 contracts > 2 allowed" in a.block_reason
    assert b.status == "refused" and "premium $600 > 50% of the account" in b.block_reason
    assert h.messenger.sent == [] and not [c for c in h.broker.calls if c[0] == "place"] and h.ex.counts["refused"] == 2
    assert [n for n in h.notes if n[1].startswith("PAPER refused")]


@pytest.mark.asyncio
async def test_caps_re_checked_with_the_live_ask_after_approval(tmp_path: Path):
    """The mark moves up while Ryan thinks: 2 contracts at 1.16 ($232) were fine, at 2.60 ($520) they breach 50 %."""
    h = Harness(tmp_path, account=1000.0)
    await h.ex.on_engine_events([open_event(contracts=2)], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    h.broker.quotes[OCC] = (2.50, 2.60)
    await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    await h.settle()
    assert t.status == "refused" and "premium $520" in t.block_reason and not [c for c in h.broker.calls if c[0] == "place"]


def test_auto_mode_is_refused_until_m6(tmp_path: Path):
    assert ALLOW_AUTO_MODE is False
    with pytest.raises(ValueError, match="M6"):
        Harness(tmp_path, mode="auto")


# ----------------------------------------------------------------------------------------------------- exits
@pytest.mark.asyncio
async def test_exit_escalates_to_the_flatten_ladder_when_the_exit_ladder_fails(tmp_path: Path):
    h = Harness(tmp_path, broker=FakeBroker(mode="never", market_fills=True))
    # entry: the 'never' broker needs a nudge — fill the entry by hand through a one-off at_limit mode
    h.broker.mode = "at_limit"
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    await h.settle()
    assert t.status == "open"
    h.broker.mode = "never"
    await h.ex.on_engine_events([close_event(reason="time_stop")], feed_mode="realtime")
    await h.run_until(at(10, 1, 0))
    assert t.status == "closed" and t.exit_qty == 1 and t.exit_price == 1.12 and t.exit_reason == "time_stop"   # market at the bid
    assert [x["kind"] for x in t.exits] == ["exit", "flatten"] and any("flattening 1 at market" in n for n in t.notes)
    assert await h.broker.positions() == []


@pytest.mark.asyncio
async def test_bank_event_sells_part_of_the_position(tmp_path: Path):
    h = Harness(tmp_path, account=5000.0)                 # room for 3 contracts under the caps
    await h.ex.on_engine_events([open_event(contracts=3, mode="kelly")], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    await h.settle()
    assert t.status == "open" and t.entry_qty == 3
    h.broker.quotes[OCC] = (1.60, 1.64)
    await h.ex.on_engine_events([{"type": "bank", "at": at(9, 45, 2).isoformat(), "position": t.engine_key, "symbol": "SPY", "contracts": 2, "bid": 1.60}],
                                feed_mode="realtime")
    await h.run_until(at(9, 45, 30))
    assert t.status == "open" and t.exit_qty == 2 and t.remaining == 1 and t.exit_price == 1.62 and h.ex.counts["banks"] == 1
    h.broker.quotes[OCC] = (2.00, 2.04)
    await h.ex.on_engine_events([close_event(reason="trail", exit_bid=2.00, r=1.2)], feed_mode="realtime")
    await h.run_until(at(9, 55, 0))
    assert t.status == "closed" and t.exit_qty == 3 and t.exit_price == pytest.approx((2 * 1.62 + 2.02) / 3, abs=1e-4)
    assert t.realized_pnl == pytest.approx((2 * 1.62 + 2.02) * 100 - 3 * 1.14 * 100, abs=0.01)


# -------------------------------------------------------------------------------------------------- kill switch
@pytest.mark.asyncio
async def test_halt_flattens_within_ten_seconds_and_blocks_new_entries(tmp_path: Path):
    h = Harness(tmp_path, broker=FakeBroker(mode="at_limit"))
    await h.ex.on_engine_events([open_event("k-open")], feed_mode="realtime")
    await h.settle()
    t = h.ex.trades["k-open"]
    await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    await h.settle()
    assert t.status == "open"
    # a second proposal is pending, a third entry is working on a book that never fills, and a stranger position sits in the sandbox
    await h.ex.on_engine_events([open_event("k-pending", option_symbol=".QQQ260928C580")], feed_mode="realtime")
    await h.settle()
    h.broker.quotes["QQQ   260928C00580000"] = (1.00, 1.10)
    h.broker.quotes["IWM   260928C00240000"] = (0.50, 0.60)
    h.broker._positions["IWM   260928C00240000"] = [1, 0.55]
    h.broker.mode = "never"
    await h.ex.on_engine_events([open_event("k-working", option_symbol=".QQQ260928C580")], feed_mode="realtime")
    await h.settle()
    tw = h.ex.trades["k-working"]
    await h.approvals.resolve(tw.proposal_id, "ok", "Ryan")
    await h.settle()
    assert tw.status == "working"
    started = h.clock.now()
    halt_task = asyncio.create_task(h.ex.halt("test halt", "Ryan"))
    await h.run_until(started + timedelta(seconds=30))
    rec = halt_task.result()
    assert rec["ok"] and rec["seconds"] <= 10 and rec["within_budget"] and rec["closed"] == ["k-open"] and len(rec["cancelled"]) == 1
    assert rec["unknown_flattened"] == ["IWM   260928C00240000"]
    assert t.status == "closed" and t.exit_reason == "halt by Ryan" and t.exits[-1]["kind"] == "flatten" and t.exit_price == 1.12
    assert tw.status == "cancelled" and h.ex.trades["k-pending"].status == "expired"
    assert await h.broker.positions() == [] and h.kill.engaged and h.halt_setting == [True]
    assert any(n[1].startswith("HALT ▸ by Ryan (test halt): flat in") for n in h.notes)
    # halted: a new gate fire is blocked, nothing is proposed
    await h.ex.on_engine_events([open_event("k-after")], feed_mode="realtime")
    await h.settle()
    assert h.ex.trades["k-after"].status == "blocked" and h.ex.trades["k-after"].block_reason.startswith("halt:")
    assert h.ex.counts["halts"] == 1 and h.ex.summary()["halted"] is True
    # resume clears the file flag and the mirrored setting
    assert await h.ex.resume("Ryan") and not h.kill.engaged and h.halt_setting == [True, False]


@pytest.mark.asyncio
async def test_halt_file_flag_survives_a_restart(tmp_path: Path):
    h = Harness(tmp_path)
    h.kill.engage("manual", "Ryan", h.clock.now())
    k2 = KillSwitch(tmp_path / "state")
    assert k2.engaged and k2.info()["by"] == "Ryan" and k2.info()["reason"] == "manual"
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    assert next(iter(h.ex.trades.values())).status == "blocked"


# ------------------------------------------------------------------------------------------------ reconciliation
@pytest.mark.asyncio
async def test_reconcile_matches_the_book_and_flags_strangers(tmp_path: Path):
    h = Harness(tmp_path)
    rec = await h.ex.reconcile()
    assert rec["ok"] and rec["mismatches"] == [] and rec["open_trades"] == 0
    await h.ex.on_engine_events([open_event()], feed_mode="realtime")
    await h.settle()
    t = next(iter(h.ex.trades.values()))
    await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    await h.settle()
    rec = await h.ex.reconcile()
    assert rec["ok"] and rec["local_positions"] == {OCC: 1} and rec["broker_positions"][0]["quantity"] == 1
    # a stranger position and a stranger order appear in the sandbox
    h.broker._positions["IWM   260928C00240000"] = [2, 0.55]
    h.broker.quotes["IWM   260928C00240000"] = (0.50, 0.60)
    h.broker.mode = "never"
    await h.broker.place("IWM   260928C00240000", "buy_to_open", 1, 0.50)
    rec = await h.ex.reconcile()
    assert not rec["ok"] and [m["kind"] for m in rec["mismatches"]] == ["position", "unknown_live_order"]
    assert rec["mismatches"][0] == {"symbol": "IWM   260928C00240000", "kind": "position", "broker": 2, "local": 0}
    assert h.notes[-1][0] == "system" and h.notes[-1][1].startswith("RECONCILE ⚠ broker ≠ book: IWM   260928C00240000 position broker 2 / book 0")
    n_notes = len(h.notes)
    await h.ex.reconcile()
    assert len(h.notes) == n_notes                                  # the same mismatch is alerted once
    rows = h.store.reconciliations(D.isoformat())
    assert [r["ok"] for r in rows] == [1, 1, 0, 0] and h.ex.counts["reconciliations"] == 4 and h.ex.counts["mismatches"] == 2


@pytest.mark.asyncio
async def test_end_of_day_expires_and_closes(tmp_path: Path):
    h = Harness(tmp_path)
    await h.ex.on_engine_events([open_event("k-open")], feed_mode="realtime")
    await h.settle()
    t = h.ex.trades["k-open"]
    await h.approvals.resolve(t.proposal_id, "ok", "Ryan")
    await h.settle()
    await h.ex.on_engine_events([open_event("k-pending", option_symbol=".QQQ260928C580")], feed_mode="realtime")
    await h.settle()
    eod = asyncio.create_task(h.ex.end_of_day())
    await h.run_until(h.clock.now() + timedelta(seconds=30))
    assert eod.done() and t.status == "closed" and t.exit_reason == "session end" and h.ex.trades["k-pending"].status == "expired"
    s = h.ex.summary()
    assert s["closed"] == 1 and s["open"] == 0 and s["by_status"] == {"closed": 1, "expired": 1} and s["approvals"]["expired"] == 1
