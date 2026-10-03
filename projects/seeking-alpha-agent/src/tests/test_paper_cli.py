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
    assert rt.realized_pnl is None and store.paper_trades(D.isoformat())[0]["status"] == "unfilled" and rt.note == ""


@pytest.mark.asyncio
async def test_roundtrip_and_halt_test_explain_an_order_parked_for_the_next_session(tmp_path: Path):
    """Outside regular hours the sandbox accepts an order with `tif.next_valid_session` and never fills it (Ryan's Saturday
    run): the self-tests say so instead of a bare 'unfilled'."""
    from saa_daemon.paper import QUEUED_WARNING, queued_note
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="never")
    b.now_fn = clock.now
    b.quotes[OCC] = (4.75, 4.77)
    b.warnings = [f"{QUEUED_WARNING}: Your order will begin working during next valid session."]
    store = Store(tmp_path / "saa.sqlite")
    task = asyncio.create_task(roundtrip(b, store, clock, SYM, (4.75, 4.77), run_id="rt-test", trade_date=D))
    await _drive(clock, task, at(10, 2))
    rt = task.result()
    assert not rt.ok and rt.entry["status"] == "unfilled" and rt.reconciled
    assert rt.note.startswith("the sandbox queued the order for the next session") and "09:30 and 16:00 ET" in rt.note
    assert rt.entry["warnings"] == b.warnings and rt.entry["limit_prices"] == [4.76, 4.77]      # SPY ladders in pennies above $3
    assert rt.note in store.paper_trades(D.isoformat())[0]["payload"]["notes"] and rt.as_dict()["note"] == rt.note
    out_task = asyncio.create_task(halt_test(b, store, clock, SYM, (4.75, 4.77), run_id="ht-test", trade_date=D, state_dir=tmp_path / "state"))
    await _drive(clock, out_task, at(10, 4))
    out = out_task.result()
    assert not out["ok"] and out["note"].startswith("entry did not fill (unfilled:") and "queued the order for the next session" in out["note"]
    # a filled order carries no such note even when the broker warned
    filled = FakeBroker(mode="at_limit")
    filled.warnings = list(b.warnings)
    o = await filled.place(OCC, "buy_to_open", 1, 4.76)
    from saa_daemon.execution import Ticket
    t = Ticket.new("k", SYM, "buy", 1, 4.75, 4.77, clock.now())
    t.warnings, t.filled_quantity = list(o.warnings), 1
    assert queued_note(t) == ""


def test_cli_hours_reason_is_blank_only_inside_a_trading_session(tmp_path: Path):
    from saa_daemon.clock import Schedule
    from saa_daemon.paper_cli import _Ctx
    p = tmp_path / ".env"
    p.write_text("TT_PROD_CLIENT_ID=x\nSAA_MIRROR=false\n", encoding="utf-8")
    settings = config.load_settings(p, environ={}, state_dir=tmp_path / "state")
    c = _Ctx(settings, need_broker=False, need_data=False)
    sat = date(2026, 10, 3)
    c.clock, c.today, c.sched = FakeClock(et_dt(sat, time(11, 32))), sat, Schedule.for_date(sat)
    assert c.hours_reason().startswith("Sat 2026-10-03 is not a trading day") and "09:30–16:00 ET" in c.hours_reason()
    mon = date(2026, 10, 5)
    c.today, c.sched = mon, Schedule.for_date(mon)
    for hh, mm, inside in ((9, 29, False), (9, 30, True), (12, 0, True), (15, 59, True), (16, 0, False), (17, 30, False)):
        c.clock = FakeClock(et_dt(mon, time(hh, mm)))
        why = c.hours_reason()
        assert (why == "") is inside, (hh, mm, why)
        if not inside:
            assert why.startswith(f"{hh:02d}:{mm:02d} ET is outside regular hours (09:30–16:00)")
    # an early-close day closes at 13:00
    nov27 = date(2026, 11, 27)
    c.today, c.sched, c.clock = nov27, Schedule.for_date(nov27), FakeClock(et_dt(nov27, time(13, 0)))
    assert c.sched.early_close and c.hours_reason().startswith("13:00 ET is outside regular hours (09:30–13:00)")


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


# ----------------------------------------------------------------------------------- instrument choice (sandbox-aware)
class _Strike:
    def __init__(self, k: float, und: str, code: str):
        self.strike_price = k
        self.call_streamer_symbol = f".{und}{code}C{k:g}"
        self.put_streamer_symbol = f".{und}{code}P{k:g}"


class _Exp:
    def __init__(self, d: date, today: date, strikes: list[float], und: str):
        self.expiration_date, self.days_to_expiration = d, (d - today).days
        self.strikes = [_Strike(k, und, f"{d:%y%m%d}") for k in strikes]


class _Nested:
    def __init__(self, exps: list[_Exp]):
        self.option_chain_type = "Standard"
        self.expirations = exps


class ChainBrokerage:
    """Production chain vs the sandbox's own chain (None = the sandbox lists nothing; `fail_sandbox` = the lookup raises)."""

    def __init__(self, prod: dict[date, list[float]], sand: dict[date, list[float]] | None, *, spot: float = 769.72,
                 today: date, fail_sandbox: bool = False, sandbox: bool = True):
        self.data, self.broker = object(), (object() if sandbox else None)
        self.prod, self.sand, self.spot, self.today, self.fail_sandbox = prod, sand, spot, today, fail_sandbox
        self.chain_calls: list[str] = []

    async def spot_prices(self, symbols):
        return {s: self.spot for s in symbols}

    async def nested_chain(self, underlying: str, *, session=None):
        self.chain_calls.append("prod" if session is None else "sandbox")
        if session is None:
            return _Nested([_Exp(d, self.today, ks, underlying) for d, ks in self.prod.items()])
        if self.fail_sandbox:
            raise RuntimeError("404: Couldn't parse response")
        return None if self.sand is None else _Nested([_Exp(d, self.today, ks, underlying) for d, ks in self.sand.items()])


MON, WED, FRI = date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 9)
T = date(2026, 10, 3)                                              # a Saturday: everything listed is in the future
FINE = [float(k) for k in range(760, 781)]                         # production: $1 strikes
COARSE = [760.0, 765.0, 770.0, 775.0, 780.0]                       # sandbox: $5 strikes


async def _probe(sym: str):
    return (2.02, 2.04)


@pytest.mark.asyncio
async def test_choose_entry_intersects_the_sandbox_chain():
    from saa_daemon.paper import choose_entry
    brk = ChainBrokerage({MON: FINE, WED: FINE, FRI: FINE}, {WED: COARSE, FRI: COARSE}, today=T)
    fake = FakeBroker(mode="at_limit")
    seen: list[str] = []

    async def lookup(occ: str):
        seen.append(occ)
        return None

    pick = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe, lookup=lookup, dry_run=fake.dry_run)
    # the Monday expiration production lists but the sandbox does not is never a candidate; 770 is the nearest common strike
    assert pick.symbol == ".SPY261007C770" and pick.occ == "SPY   261007C00770000" and pick.expiration == WED and pick.strike == 770.0
    assert pick.quote == (2.02, 2.04) and pick.spot == 769.72 and seen == [pick.occ]
    assert fake.calls == [("dry_run", pick.occ, "buy_to_open", 1, 2.03)]          # the exact entry order, ladder rung 0
    assert brk.chain_calls == ["prod", "sandbox"]
    text = "\n".join(pick.lines)
    assert "production chain: 3 live expirations (2026-10-05 … 2026-10-09)" in text
    assert "sandbox chain: 2 live expirations of 2 listed (2026-10-07 … 2026-10-09)" in text
    assert "common live expirations: 2 (first 2026-10-07)" in text and "chosen .SPY261007C770 = SPY   261007C00770000" in text
    assert "sandbox dry run accepted 1 @ 2.03" in text
    d = pick.as_dict()
    assert d["expiration"] == "2026-10-07" and d["quote"] == [2.02, 2.04] and d["lines"] == pick.lines


@pytest.mark.asyncio
async def test_choose_entry_skips_what_the_sandbox_refuses():
    from saa_daemon.paper import choose_entry
    brk = ChainBrokerage({MON: FINE, WED: FINE}, {MON: FINE, WED: FINE}, today=T)
    fake = FakeBroker(mode="at_limit", untradable={"SPY   261005C00769000"})   # the dry run refuses Monday's 769

    async def lookup(occ: str):
        return "closing-only in the sandbox" if occ == "SPY   261005C00770000" else None

    quotes = {".SPY261005C771": None}                                           # Monday's 771 has no two-sided quote

    async def probe(sym: str):
        return quotes.get(sym, (2.02, 2.04))

    pick = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=probe, lookup=lookup, dry_run=fake.dry_run)
    # nearest-ATM order at spot 769.72 on Monday: 770 (lookup refuses), 769 (dry run refuses), 771 (no quote) — three
    # strikes per expiration, then the next expiration: Wednesday's 770 passes
    assert pick.symbol == ".SPY261007C770" and pick.expiration == WED and pick.strike == 770.0
    text = "\n".join(pick.lines)
    assert "skip .SPY261005C770: closing-only in the sandbox" in text
    assert "skip .SPY261005C769: sandbox dry run refused: instrument_validation_failed: Trading of SPY   261005C00769000 is not supported" in text
    assert "skip .SPY261005C771: no two-sided DXLink quote within the window" in text
    assert [c[1] for c in fake.calls] == ["SPY   261005C00769000", "SPY   261007C00770000"]


@pytest.mark.asyncio
async def test_choose_entry_falls_back_when_the_sandbox_chain_is_unreadable():
    from saa_daemon.paper import choose_entry
    brk = ChainBrokerage({MON: FINE, WED: FINE}, None, today=T, fail_sandbox=True)
    pick = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe)
    assert pick.symbol == ".SPY261005C770" and pick.expiration == MON
    assert any(l.startswith("sandbox chain lookup failed (RuntimeError: 404") and l.endswith("falling back to the production chain") for l in pick.lines)
    brk2 = ChainBrokerage({MON: FINE}, None, today=T)                           # the sandbox lists no chain at all
    pick2 = await choose_entry(brk2, "SPY", T, now_et=time(11, 0), probe=_probe)
    assert pick2.symbol == ".SPY261005C770" and any("lists no option chain for SPY" in l for l in pick2.lines)
    brk3 = ChainBrokerage({MON: FINE}, None, today=T, sandbox=False)            # no sandbox session at all
    pick3 = await choose_entry(brk3, "SPY", T, now_et=time(11, 0), probe=_probe)
    assert pick3.symbol == ".SPY261005C770" and "no sandbox session — candidates from the production chain only" in pick3.lines


@pytest.mark.asyncio
async def test_choose_entry_reports_a_stale_sandbox_chain_and_gives_up_cleanly():
    from saa_daemon.paper import choose_entry, option_candidates
    stale = {date(2026, 9, 18): COARSE, date(2026, 9, 25): COARSE}             # the sandbox only knows expired contracts
    brk = ChainBrokerage({MON: FINE, WED: FINE}, stale, today=T)
    with pytest.raises(RuntimeError) as ei:
        await option_candidates(brk, "SPY", T, now_et=time(11, 0))
    msg = str(ei.value)
    assert msg.startswith("no tradable option candidate for SPY:")
    assert "sandbox chain: 0 live expirations of 2 listed (2026-09-18 … 2026-09-25)" in msg
    assert "common live expirations: 0 — the sandbox knows none of production's live expirations/strikes" in msg and "--symbol" in msg
    # every candidate refused → the error carries the whole diagnosis
    brk2 = ChainBrokerage({MON: FINE}, {MON: FINE}, today=T)
    fake = FakeBroker(mode="reject", reject_reason="instrument_validation_failed: nope")
    with pytest.raises(RuntimeError) as ei2:
        await choose_entry(brk2, "SPY", T, now_et=time(11, 0), probe=_probe, dry_run=fake.dry_run, limit=3)
    assert str(ei2.value).count("sandbox dry run refused: instrument_validation_failed: nope") == 3 and len(fake.calls) == 3
    # a same-day expiration is live before the close and gone after it
    brk3 = ChainBrokerage({T: FINE, MON: FINE}, {T: FINE, MON: FINE}, today=T)
    assert (await choose_entry(brk3, "SPY", T, now_et=time(15, 59), probe=_probe)).expiration == T
    assert (await choose_entry(brk3, "SPY", T, now_et=time(16, 0), probe=_probe)).expiration == MON


# ------------------------------------------------------------------- the sandbox's fill rule (limits fill only under $3)
# developer.tastytrade.com/docs/sandbox: a limit order priced under $3 fills immediately, a limit order at $3 or above
# goes Live and never fills, a market order fills at $1. The at-the-money SPY call (4.75 / 4.77 on Ryan's Saturday run)
# can therefore never fill — the self-tests have to walk out of the money to a strike under the price cap.
CHAIN_1009 = {770.0: (4.75, 4.77), 771.0: (4.10, 4.12), 772.0: (3.50, 3.52), 773.0: (2.94, 2.96), 774.0: (2.43, 2.45), 775.0: (1.96, 1.98),
              776.0: (1.55, 1.57), 777.0: (1.20, 1.22), 778.0: (0.91, 0.93), 779.0: (0.68, 0.70), 780.0: (0.49, 0.51)}


def _quotes_1009() -> dict[str, tuple[float, float]]:
    return {f".SPY261009C{k:g}": q for k, q in CHAIN_1009.items()}


def test_selftest_price_cap_sits_inside_the_sandbox_rule_and_the_tier1_floor():
    from dataclasses import replace
    from saa_daemon.engine.tier1 import TIER1
    from saa_daemon.execution.broker import SANDBOX_LIMIT_FILLS_BELOW
    from saa_daemon.paper import price_cap_reason, selftest_price_cap
    assert TIER1.floor_premium_max == 150.0 and selftest_price_cap() == 1.50          # the engine's one-contract floor binds today
    assert selftest_price_cap() < SANDBOX_LIMIT_FILLS_BELOW
    assert selftest_price_cap(replace(TIER1, floor_premium_max=500.0)) == 2.99        # a richer floor: the sandbox rule binds
    why = price_cap_reason()
    assert "only under 3.00" in why and "market order fills at 1.00" in why and "one-contract floor" in why and "1.50" in why


@pytest.mark.asyncio
async def test_choose_entry_walks_out_of_the_money_to_the_first_strike_under_the_cap():
    from saa_daemon.paper import choose_entry, price_cap_reason, selftest_price_cap
    brk = ChainBrokerage({MON: FINE, FRI: FINE}, {FRI: FINE}, today=T)             # the sandbox knows Friday only (D24)
    fake = FakeBroker(mode="sandbox")
    looked: list[str] = []
    batches: list[list[str]] = []

    async def lookup(occ: str):
        looked.append(occ)
        return None

    async def probe_many(symbols: list[str]):
        batches.append(list(symbols))
        book = _quotes_1009()
        return {s: book[s] for s in symbols if s in book}

    async def never(sym: str):                                                      # the one-by-one probe is not used when a batch probe exists
        raise AssertionError(f"single probe called for {sym}")

    pick = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=never, probe_many=probe_many, lookup=lookup, dry_run=fake.dry_run,
                              max_ask=selftest_price_cap(), max_ask_why=price_cap_reason())
    assert pick.symbol == ".SPY261009C777" and pick.occ == "SPY   261009C00777000" and pick.strike == 777.0 and pick.expiration == FRI
    assert pick.quote == (1.20, 1.22) and pick.max_ask == 1.50 and pick.as_dict()["max_ask"] == 1.50
    # one batch: Friday's strikes from the at-the-money strike outward (never the in-the-money side, which only costs more)
    assert len(batches) == 1 and batches[0][0] == ".SPY261009C770" and batches[0] == [f".SPY261009C{k}" for k in range(770, 781)]
    assert looked == [pick.occ]                                                     # dearer strikes cost no sandbox calls
    assert fake.calls == [("dry_run", pick.occ, "buy_to_open", 1, 1.21)]            # the exact entry order, ladder rung 0
    text = "\n".join(pick.lines)
    assert "price cap: ask ≤ 1.50 — " in text and "only under 3.00" in text
    assert "quotes: 11 of 11 candidate strikes two-sided on DXLink" in text
    assert "over the cap (2026-10-09): 770 @ 4.77 · 771 @ 4.12 · 772 @ 3.52 · 773 @ 2.96 · 774 @ 2.45 · 775 @ 1.98 · 776 @ 1.57" in text
    assert pick.lines[-1] == ("chosen .SPY261009C777 = SPY   261009C00777000 · exp 2026-10-09 strike 777 · bid 1.20 / ask 1.22"
                              " · sandbox dry run accepted 1 @ 1.21")
    # the sandbox lists $5 strikes only: 770 and 775 are over the cap, 780 is the pick
    brk5 = ChainBrokerage({FRI: FINE}, {FRI: COARSE}, today=T)
    pick5 = await choose_entry(brk5, "SPY", T, now_et=time(11, 0), probe=never, probe_many=probe_many, dry_run=FakeBroker(mode="sandbox").dry_run,
                               max_ask=1.50)
    assert pick5.strike == 780.0 and pick5.quote == (0.49, 0.51) and batches[-1] == [".SPY261009C770", ".SPY261009C775", ".SPY261009C780"]
    assert "over the cap (2026-10-09): 770 @ 4.77 · 775 @ 1.98" in "\n".join(pick5.lines)
    # without a batch probe the strikes are probed one at a time, in the same order, with the same result
    asked: list[str] = []

    async def one(sym: str):
        asked.append(sym)
        return _quotes_1009().get(sym)

    pick1 = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=one, dry_run=FakeBroker(mode="sandbox").dry_run, max_ask=1.50)
    assert pick1.symbol == pick.symbol and asked == [f".SPY261009C{k}" for k in range(770, 778)]


@pytest.mark.asyncio
async def test_choose_entry_walks_a_long_way_when_the_nearest_common_expiration_is_dear():
    """A monthly six weeks out: the at-the-money call costs ~$15 and the first strike under the cap is 30 strikes away.
    One batch of quotes covers the walk and the passed-over strikes are summarised, not listed one per line."""
    from saa_daemon.paper import choose_entry
    nov = date(2026, 11, 20)
    wide = [float(k) for k in range(740, 841)]                                      # $1 strikes 740 … 840
    brk = ChainBrokerage({nov: wide}, {nov: wide}, today=T)
    book = {f".SPY261120C{k}": (round(15.00 - 0.45 * (k - 770), 2), round(15.04 - 0.45 * (k - 770), 2)) for k in range(770, 803)}   # 800 → 1.50 / 1.54
    asked: list[int] = []

    async def probe_many(symbols: list[str]):
        asked.append(len(symbols))
        return {s: book[s] for s in symbols if s in book}

    fake = FakeBroker(mode="sandbox")
    pick = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe, probe_many=probe_many, dry_run=fake.dry_run, max_ask=1.50)
    assert asked == [60] and pick.strike == 801.0 and pick.quote == (1.05, 1.09)
    line = next(l for l in pick.lines if l.startswith("over the cap"))
    assert line == "over the cap (2026-11-20): 770 @ 15.04 · 771 @ 14.59 · 772 @ 14.14 · … 26 more … · 799 @ 1.99 · 800 @ 1.54"
    assert sum(1 for l in pick.lines if l.startswith("skip ")) == 0 and pick.lines[-1].startswith("chosen .SPY261120C801")


@pytest.mark.asyncio
async def test_choose_entry_under_the_cap_falls_through_what_the_sandbox_refuses():
    from saa_daemon.paper import choose_entry
    brk = ChainBrokerage({FRI: FINE}, {FRI: FINE}, today=T)
    fake = FakeBroker(mode="sandbox", untradable={"SPY   261009C00778000"})         # e.g. buying power: the dry run says no

    async def lookup(occ: str):
        return "closing-only in the sandbox" if occ == "SPY   261009C00777000" else None

    async def probe_many(symbols: list[str]):
        book = _quotes_1009()
        return {s: book[s] for s in symbols if s in book}

    pick = await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe, probe_many=probe_many, lookup=lookup, dry_run=fake.dry_run, max_ask=1.50)
    assert pick.strike == 779.0 and pick.quote == (0.68, 0.70)
    text = "\n".join(pick.lines)
    assert "skip .SPY261009C777: closing-only in the sandbox" in text
    assert "skip .SPY261009C778: sandbox dry run refused: instrument_validation_failed" in text
    assert [c[1] for c in fake.calls] == ["SPY   261009C00778000", "SPY   261009C00779000"]


@pytest.mark.asyncio
async def test_choose_entry_says_so_when_nothing_is_under_the_cap_or_the_probe_fails():
    from saa_daemon.paper import choose_entry
    rich = [770.0, 771.0, 772.0]                                                     # both chains list only strikes that cost more than the cap
    brk = ChainBrokerage({FRI: rich}, {FRI: rich}, today=T)

    async def probe_many(symbols: list[str]):
        book = _quotes_1009()
        return {s: book[s] for s in symbols if s in book}

    fake = FakeBroker(mode="sandbox")
    with pytest.raises(RuntimeError) as ei:
        await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe, probe_many=probe_many, dry_run=fake.dry_run, max_ask=1.50)
    msg = str(ei.value)
    assert msg.startswith("no candidate passed the sandbox checks:") and "price cap: ask ≤ 1.50" in msg
    assert "over the cap (2026-10-09): 770 @ 4.77 · 771 @ 4.12 · 772 @ 3.52" in msg and "--symbol" in msg and fake.calls == []

    async def broken(symbols: list[str]):
        raise OSError("dxlink: connection refused\n")

    with pytest.raises(RuntimeError) as ei2:
        await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe, probe_many=broken, dry_run=fake.dry_run, max_ask=1.50)
    assert "quote probe failed (OSError: dxlink: connection refused)" in str(ei2.value) and fake.calls == []

    async def silent(symbols: list[str]):                                           # connected, but nothing quoted two-sided in the window
        return {}

    with pytest.raises(RuntimeError) as ei3:
        await choose_entry(brk, "SPY", T, now_et=time(11, 0), probe=_probe, probe_many=silent, dry_run=fake.dry_run, max_ask=1.50)
    msg3 = str(ei3.value)
    assert "quotes: 0 of 3 candidate strikes two-sided on DXLink" in msg3 and "no strike was quoted two-sided within the window" in msg3
    assert "skip " not in msg3 and fake.calls == []                                 # one line, not one per strike


@pytest.mark.asyncio
async def test_roundtrip_and_halt_test_fill_in_the_sandbox_only_under_three_dollars(tmp_path: Path):
    """The whole point of the cap: on the sandbox's fill rule Saturday's pick stays unfilled on any day, the capped pick
    completes the round trip, and the kill switch is flat on its first order."""
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="sandbox", account_masked="…9103")
    b.now_fn = clock.now
    store = Store(tmp_path / "saa.sqlite")
    task = asyncio.create_task(roundtrip(b, store, clock, ".SPY261009C770", (4.75, 4.77), run_id="rt-rich", trade_date=D))
    await _drive(clock, task, at(10, 2))
    rich = task.result()
    assert not rich.ok and rich.entry["status"] == "unfilled" and rich.entry["limit_prices"] == [4.76, 4.77] and rich.exit["status"] == "skipped"
    rt = await roundtrip(b, store, clock, ".SPY261009C777", (1.20, 1.22), run_id="rt-cheap", trade_date=D, seq=1)
    assert rt.ok and rt.reconciled and rt.entry["avg_fill_price"] == 1.21 and rt.exit["avg_fill_price"] == 1.21 and rt.realized_pnl == 0.0
    task = asyncio.create_task(halt_test(b, store, clock, ".SPY261009C777", (1.20, 1.22), run_id="ht-cheap", trade_date=D, state_dir=tmp_path / "state"))
    await _drive(clock, task, at(10, 4))
    out = task.result()
    assert out["ok"] and out["within_10s"] and out["seconds"] <= 1.0 and out["position_after"] == [] and out["trade"]["exit_price"] == 1.20
    assert out["halt"]["closed"] and not KillSwitch(tmp_path / "state").engaged


@pytest.mark.asyncio
async def test_roundtrips_in_the_same_second_keep_separate_evidence_rows(tmp_path: Path):
    """Three round trips can finish inside one wall-clock second (the sandbox fills at once): each still gets its own
    `paper_trades` row and its own order tickets, because the CLI numbers them."""
    clock = FakeClock(at(10, 0))
    b = FakeBroker(mode="sandbox")
    b.now_fn = clock.now
    store = Store(tmp_path / "saa.sqlite")
    for i in (1, 2, 3):
        rt = await roundtrip(b, store, clock, ".SPY261009C777", (1.20, 1.22), run_id="rt-3", trade_date=D, seq=i)
        assert rt.ok
    rows = store.paper_trades(D.isoformat())
    assert len(rows) == 3 and len({r["payload"]["engine_key"] for r in rows}) == 3 and all(r["status"] == "closed" for r in rows)
    assert sorted(r["payload"]["engine_key"] for r in rows) == [f"roundtrip|.SPY261009C777|100000|{i}" for i in (1, 2, 3)]
    assert len(store.paper_orders(D.isoformat())) == 6


# ------------------------------------------------------------------------- the CLI commands end to end, offline
def _cli_settings(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("TT_PROD_CLIENT_ID=x\nSAA_MIRROR=false\n", encoding="utf-8")
    return config.load_settings(p, environ={}, state_dir=tmp_path / "state")


def _offline_cli(monkeypatch, clock: FakeClock, day: date, broker: FakeBroker, *, feed_mode: str = "DELAYED"):
    """Swap the CLI's network edges for fakes: the two tastytrade sessions (a chain-serving brokerage and a FakeBroker on
    the sandbox's fill rule), the DXLink probes and the sandbox instrument lookup. Everything between — the feed gate, the
    instrument choice, the ladder, the kill switch, the evidence rows and every printed line — is the real code."""
    from saa_daemon import paper_cli
    from saa_daemon.clock import Schedule
    from saa_daemon.paper import FeedCheck

    probes: list[list[str]] = []

    class OfflineCtx(paper_cli._Ctx):
        async def __aenter__(self):
            self.clock, self.today, self.sched = clock, day, Schedule.for_date(day)
            self.brokerage = ChainBrokerage({MON: FINE, FRI: FINE}, {FRI: FINE}, today=day)
            self.broker = broker
            return self

        async def __aexit__(self, *exc):
            await self.http.aclose()

    async def feed(_session, _symbols, **_kw):
        note = {"DELAYED": "8 trades, median 912.4s", "realtime": "8 trades, median 0.4s"}[feed_mode]
        return FeedCheck(feed_mode, 912.4 if feed_mode == "DELAYED" else 0.4, note)

    async def probe_many(_session, symbols, **_kw):
        probes.append(list(symbols))
        book = _quotes_1009()
        return {s: book[s] for s in symbols if s in book}

    def lookup(_session):
        async def _lookup(_occ: str):
            return None
        return _lookup

    monkeypatch.setattr(paper_cli, "_Ctx", OfflineCtx)
    monkeypatch.setattr(paper_cli, "measure_feed_lag", feed)
    monkeypatch.setattr(paper_cli, "probe_option_quotes", probe_many)
    monkeypatch.setattr(paper_cli, "sandbox_lookup", lookup)
    return probes


@pytest.mark.asyncio
async def test_cli_paper_roundtrip_prints_what_the_m4_pass_rule_asks_for(tmp_path: Path, monkeypatch, capsys):
    """`./run.sh paper-roundtrip --n 3 --allow-delayed` on Monday 10:05 ET, against a broker that behaves as the sandbox
    is documented to: the pick line, three filled-and-reconciled round trips, the RESULT line — the rule Ryan grades by."""
    from saa_daemon.paper_cli import run_roundtrip
    clock = FakeClock(et_dt(MON, time(10, 5)))
    broker = FakeBroker(mode="sandbox", account_masked="…9103")
    broker.now_fn = clock.now
    probes = _offline_cli(monkeypatch, clock, MON, broker)
    settings = _cli_settings(tmp_path)
    task = asyncio.create_task(run_roundtrip(settings, symbol="SPY", n=3, allow_delayed=True))
    await _drive(clock, task, et_dt(MON, time(10, 10)))            # an unfilled ladder would wait on the virtual clock: drive it, then fail on the result
    assert task.result() == 0
    out = capsys.readouterr().out.splitlines()
    head = out.index("choosing an option on SPY the sandbox trades:")
    spot = next(i for i, l in enumerate(out) if l.startswith("SPY spot "))
    pick_lines = out[head + 1:spot]
    assert all(l.startswith("  ") for l in pick_lines) and pick_lines[-1].startswith("  chosen .SPY261009C777 = SPY   261009C00777000")
    assert pick_lines[-1].endswith("sandbox dry run accepted 1 @ 1.21") and any("price cap: ask ≤ 1.50" in l for l in pick_lines)
    assert out[spot] == "SPY spot 769.72 → .SPY261009C777 bid 1.20 / ask 1.22" and len(probes) == 1
    assert "⚠ --allow-delayed: sandbox plumbing test on a non-real-time quote; recorded as such" in out and not any("outside regular hours" in l for l in out)
    for i in (1, 2, 3):
        at_ = out.index(f"--- round trip {i}/3 ---")
        assert out[at_ + 1].startswith("entry filled: 1/1 @ 1.21 ladder [1.21]") and out[at_ + 2].startswith("exit  filled: 1/1 @ 1.21 ladder [1.21]")
        assert out[at_ + 3].startswith("reconciled: True · ")
    assert out[-1] == "RESULT: ALL ROUND TRIPS FILLED AND RECONCILED"
    store = Store(tmp_path / "state" / "saa.sqlite")
    rows = store.paper_trades(MON.isoformat())
    assert len(rows) == 3 and all(r["status"] == "closed" and r["payload"]["lane"] == "roundtrip" for r in rows)
    assert await broker.positions() == [] and await broker.live_orders() == []
    assert [c for c in broker.calls if c[0] == "dry_run"] == [("dry_run", "SPY   261009C00777000", "buy_to_open", 1, 1.21)]


@pytest.mark.asyncio
async def test_cli_paper_roundtrip_refuses_a_delayed_feed_without_the_flag(tmp_path: Path, monkeypatch, capsys):
    from saa_daemon.paper_cli import run_roundtrip
    clock = FakeClock(et_dt(MON, time(10, 5)))
    broker = FakeBroker(mode="sandbox")
    _offline_cli(monkeypatch, clock, MON, broker)
    with pytest.raises(RuntimeError, match="not measurably real-time"):
        await run_roundtrip(_cli_settings(tmp_path), symbol="SPY", n=1, allow_delayed=False)
    assert broker.calls == [] and "feed lag: DELAYED (8 trades, median 912.4s)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_halt_test_prints_what_the_m4_pass_rule_asks_for(tmp_path: Path, monkeypatch, capsys):
    """`./run.sh halt-test --allow-delayed`: one contract bought, the kill switch flattens it, `HALT: flat=True in Ns`
    with N ≤ 10 and `RESULT: FLAT WITHIN 10 S`; the switch is re-armed afterwards."""
    import re
    from saa_daemon.paper_cli import run_halt_test
    clock = FakeClock(et_dt(MON, time(10, 20)))
    broker = FakeBroker(mode="sandbox", account_masked="…9103")
    broker.now_fn = clock.now
    _offline_cli(monkeypatch, clock, MON, broker)
    settings = _cli_settings(tmp_path)
    task = asyncio.create_task(run_halt_test(settings, symbol="SPY", allow_delayed=True))
    await _drive(clock, task, et_dt(MON, time(10, 22)))
    assert task.result() == 0
    out = capsys.readouterr().out.splitlines()
    assert any(l.startswith("  chosen .SPY261009C777") and l.endswith("sandbox dry run accepted 1 @ 1.21") for l in out)
    assert any(l.startswith("entry filled: 1/1 @ 1.21") for l in out)
    halt = next(l for l in out if l.startswith("HALT: "))
    m = re.match(r"HALT: flat=True in (\d+(?:\.\d+)?)s \(budget 10 s: ✓\)", halt)
    assert m and float(m.group(1)) <= 10 and "position before [('SPY   261009C00777000', 1)] → after []" in halt
    assert out[-1] == "RESULT: FLAT WITHIN 10 S" and not KillSwitch(tmp_path / "state").engaged
    assert await broker.positions() == []


# ------------------------------------------------------------------------------------- approval test: the tap finishes
class SlowBot(FakeBot):
    """A FakeBot whose Bot-API calls take a few event-loop turns, like the network does — enough for a teardown that
    cancels the poller too early to cut the decision edit, the callback answer and the offset save short."""

    def __init__(self, clock: FakeClock, script: str | None, after_s: float = 5.0):
        super().__init__(clock, script, after_s)
        self.answers: list[str | None] = []
        self.saved: list[int] = []

    async def _net(self):
        for _ in range(8):
            await asyncio.sleep(0)

    async def edit(self, message_id, text):
        await self._net()
        self.edits.append(text)

    async def poll(self, stop, *, on_callback, on_command, load_offset=None, save_offset=None):
        if self.script is None:
            await stop.wait()
            return
        await self.clock.sleep(self.after_s)
        data = self.sent[-1]["buttons"][0 if self.script == "ok" else 1][1]
        answer = await on_callback(data, "Ryan", "cb1", 900)          # resolves the proposal, edits the message
        await self._net()
        self.answers.append(answer)                                    # answerCallbackQuery (stops the button's spinner)
        if save_offset is not None:
            await self._net()
            await save_offset(43)                                      # confirms the update so nobody is handed it again
        await stop.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("script,verdict,toast", [("ok", "✅ Approved by Ryan", "Approved ✅ (test)"), ("skip", "⏭ Skipped by Ryan", "Skipped ⏭ (test)")])
async def test_approval_test_lets_the_tap_finish_before_it_tears_the_poller_down(tmp_path: Path, script, verdict, toast):
    clock = FakeClock(at(10, 0))
    store = Store(tmp_path / "saa.sqlite")
    bot = SlowBot(clock, script)
    saved: list[int] = []

    async def save_offset(o: int):
        saved.append(o)

    task = asyncio.create_task(approval_test(bot, store, clock, timeout_s=30.0, run_id="at-test", trade_date=D, save_offset=save_offset))
    await _drive(clock, task, at(10, 1))
    out = task.result()
    assert out["ok"] and out["status"] == ("approved" if script == "ok" else "skipped") and out["decided_by"] == "Ryan"
    assert any(verdict in e for e in bot.edits), "the proposal message was never edited with the decision"
    assert bot.answers == [toast], "the button tap was never answered"
    assert saved == [43], "the update offset was never saved"


class IdlePollBot(FakeBot):
    """A poller parked in a long-poll that only a cancel ends (the real getUpdates holds the connection for 20 s)."""

    async def poll(self, stop, *, on_callback, on_command, load_offset=None, save_offset=None):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_approval_test_timeout_does_not_wait_on_an_idle_poller(tmp_path: Path):
    import time as _time
    clock = FakeClock(at(10, 0))
    bot = IdlePollBot(clock, None)
    t0 = _time.monotonic()
    task = asyncio.create_task(approval_test(bot, Store(tmp_path / "saa.sqlite"), clock, timeout_s=30.0, run_id="at-test", trade_date=D))
    await _drive(clock, task, at(10, 1))
    out = task.result()
    assert out["status"] == "timeout" and out["logged_as"] == "skip (timeout)" and any("SKIPPED (timeout)" in e for e in bot.edits)
    assert _time.monotonic() - t0 < 2.0, "the timeout path waited on a poller that had nothing in flight"
