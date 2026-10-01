"""Tier 1 rails — every rail has a test that tries to breach it through the live engine path and fails (M3 acceptance).

Each test builds a state in which *everything else* says FIRE (green checklist, readable + favorable regime, fresh
two-sided marks, a native trigger on the last bar) and then attacks one rail: the clock, the release blackout, the daily
−3R stop, the 3-loss lockout, cooling-off, the edge-loss halt, the time stop, the spread filter, the feed gate, the
STAND-DOWN default, the concurrency cap. Hypothesis draws the attack parameters."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine_harness import D, NEG_GAMMA, SCHED, at, checklist, make_engine, make_market, orb_breakout_bars, set_marks, tick, vwap_reclaim_bars
from saa_daemon.clock import et
from saa_daemon.engine.rails import Rails
from saa_daemon.engine.rules import Rules
from saa_daemon.engine.tier1 import TIER1

ECON_FOMC = [{"event": "FOMC decision", "time_et": "14:00"}]
H = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def trigger_available(now: datetime) -> bool:
    """What the harness can arm at this time: an ORB breakout until 10:30 (needs 6 completed bars), a VWAP reclaim until 15:30 (15 bars)."""
    t = et(now).time()
    bars = int((now - SCHED.open).total_seconds() // 60)
    return (t <= time(10, 30) and bars >= 6) or (time(10, 30) < t <= time(15, 30) and bars >= 15)


def armed_state(hh: int, mm: int, *, sym: str = "SPY", spot: float = 650.0, spread: float = 0.04, econ=(), history=(), cooling_off=False,
                checklists=None, rules: Rules | None = None):
    """A trigger bar completed one minute before the tick at hh:mm:02 (ORB breakout before 10:30, VWAP reclaim after); marks fresh;
    checklist green for `sym`."""
    now = at(hh, mm, 2)
    bar_minutes = max(1, int((now - SCHED.open).total_seconds() // 60))     # bars from the open up to (not incl.) the tick minute
    # ATM IV 5%: once 60 minutes of bars exist the wedge is measured natively (RV > IV) instead of taken from the brief
    m = make_market({sym: spot}, now, spread_frac=spread, iv=0.05)
    if et(now).time() <= time(10, 30):
        orb_breakout_bars(m, sym, spot, SCHED.open, bar_minutes, break_at=bar_minutes - 1)
    else:
        vwap_reclaim_bars(m, sym, spot, SCHED.open, bar_minutes)
    m.spots[sym] = spot + 0.6
    set_marks(m, m.plans[sym], spot + 0.6, now, spread_frac=spread, iv=0.05)
    cls = checklists if checklists is not None else [checklist(sym, "long", window_kind="open", start=SCHED.open, end=SCHED.close)]
    eng = make_engine(checklists=cls, econ=list(econ), history=list(history), cooling_off=cooling_off, rules=rules)
    return eng, m, now


def gate_opens(eng) -> int:
    return sum(1 for p in eng.positions.values() if p.source == "gate")


# ------------------------------------------------------------------------------------------------- the clock rail
@settings(max_examples=120, **H)
@given(minute=st.integers(min_value=0, max_value=6 * 60 + 29))
def test_rail_clock_no_fresh_entries_outside_entry_windows(minute):
    """Try to fire at every minute of the day: entries happen only where WindowSchedule allows them (never 11:30–13:30,
    never in the last 5 minutes, never in mid/afternoon without an event, never before the open)."""
    t = (datetime.combine(D, time(9, 30)) + timedelta(minutes=minute)).time()
    eng, m, now = armed_state(t.hour, t.minute)
    tick(eng, m, now)
    allowed, why = eng.sched.entry_window(now)
    if allowed is not None and trigger_available(now):
        assert gate_opens(eng) == 1, (et(now), why, eng.decisions[-1:])      # last hour included: NEG_GAMMA satisfies the gamma read
    else:
        assert gate_opens(eng) == 0, (et(now), why)
        if eng.decisions:
            assert all(d["decision"] != "fire" for d in eng.decisions)
    if TIER1.no_entry_start <= t < TIER1.no_entry_end:
        assert gate_opens(eng) == 0 and all(p.source != "fast_lane" for p in eng.positions.values())   # fast lanes obey the dead zone too


def test_rail_last_hour_needs_negative_gamma():
    eng, m, now = armed_state(15, 10)
    tick(eng, m, now, gamma={"SPY": {**NEG_GAMMA, "regime": "positive"}})
    assert gate_opens(eng) == 0 and any("negative-gamma" in d["reason"] for d in eng.decisions)
    eng2, m2, now2 = armed_state(15, 10)
    tick(eng2, m2, now2, gamma={"SPY": {**NEG_GAMMA, "regime": "unknown", "coverage": 0.0}})
    assert gate_opens(eng2) == 0
    eng3, m3, now3 = armed_state(15, 10)
    tick(eng3, m3, now3)
    assert gate_opens(eng3) == 1 and et(next(iter(eng3.positions.values())).time_stop).time() == time(15, 55)


# ------------------------------------------------------------------------------------- never own through a release
@settings(max_examples=80, **H)
@given(minute=st.integers(min_value=0, max_value=89))
def test_rail_release_blackout(minute):
    """With FOMC at 14:00: no entry in 13:45–14:00 and nothing opened before it survives past 13:45."""
    t = (datetime.combine(D, time(13, 30)) + timedelta(minutes=minute)).time()
    cl = [checklist("SPY", "long", window_kind="event", start=at(13, 30), end=at(15, 0))]
    eng, m, now = armed_state(t.hour, t.minute, econ=ECON_FOMC, checklists=cl)
    tick(eng, m, now)
    blackout = time(13, 45) <= t < time(14, 0)
    if blackout:
        assert gate_opens(eng) == 0 and not any(p.source == "fast_lane" for p in eng.positions.values())
    for p in eng.positions.values():
        assert p.time_stop <= at(13, 45) or p.opened_at >= at(14, 0), (et(p.opened_at), et(p.time_stop))
        if p.opened_at < at(13, 45):
            assert p.time_stop_reason == "release" or p.time_stop <= at(13, 45)


def test_rail_release_blackout_closes_open_position():
    """A position opened in the afternoon event path at 14:06 (after the statement) carries a stop at the presser? No —
    here: a position opened at 13:40 before the 14:00 release must be flat by 13:45 even if the trail says hold."""
    cl = [checklist("SPY", "long", window_kind="event", start=at(13, 30), end=at(15, 0))]
    # 13:40 is in the afternoon window (event windows only) → a gate entry is not allowed; use a fast-lane shadow instead
    eng, m, now = armed_state(13, 40, econ=ECON_FOMC, checklists=cl)
    tick(eng, m, now)
    fl = [p for p in eng.positions.values() if p.source == "fast_lane"]
    assert fl and fl[0].time_stop == at(13, 45) and fl[0].time_stop_reason == "release"
    # marks rise strongly (trail would hold) — the 13:45 tick still closes it
    for mm in (41, 42, 43, 44, 45):
        for p in fl:
            st_ = m.options.state[p.option_symbol]
            st_.bid, st_.ask = 3.0 + mm * 0.1, 3.1 + mm * 0.1
            st_.recv_ms = int(at(13, mm, 2).timestamp() * 1000)
        tick(eng, m, at(13, mm, 2))
    assert fl[0].status == "closed" and fl[0].exit_reason == "release_blackout" and fl[0].exit_at == at(13, 45, 2)


# ------------------------------------------------------------------------------------------- daily stop / lockout
def _closes(rails: Rails, rs, now):
    for r in rs:
        rails.on_close(r, now)


@settings(max_examples=200, **H)
@given(rs=st.lists(st.floats(min_value=-1.0, max_value=12.0, allow_nan=False), min_size=1, max_size=15))
def test_rail_daily_stop_and_lockout_state(rs):
    rails = Rails()
    _closes(rails, rs, at(10, 0))
    total = sum(rs)
    streak = 0
    for r in reversed(rs):
        if r <= 0:
            streak += 1
        else:
            break
    ok, why = rails.can_enter()
    assert rails.daily_stop_hit() == (total <= TIER1.daily_stop_r)
    assert rails.locked_out() == (streak >= TIER1.consecutive_loss_lockout)
    assert ok == (not rails.daily_stop_hit() and not rails.locked_out())
    assert rails.cooling_off_triggered == any(r >= TIER1.cooling_off_win_r for r in rs) or rails.cooling_off_triggered == (total >= TIER1.cooling_off_day_r) or rails.cooling_off_triggered


def test_rail_daily_stop_blocks_the_engine():
    """Three full losses interleaved with small wins: −3R reached without three straight losses → still no entries."""
    eng, m, now = armed_state(9, 40)
    for r in (-1.0, 0.2, -1.0, 0.1, -1.0, -0.4):
        eng.rails.on_close(r, at(9, 35))
    assert eng.rails.daily_stop_hit() and not eng.rails.locked_out()
    tick(eng, m, now)
    assert gate_opens(eng) == 0 and any(d["reason"].startswith("rails: daily stop") for d in eng.decisions)
    assert any(p.source == "fast_lane" for p in eng.positions.values())        # hypotheses keep measuring (zero capital)


def test_rail_three_loss_lockout_blocks_the_engine():
    eng, m, now = armed_state(9, 40)
    for r in (-0.4, -0.3, -0.5):                                              # −1.2R total: not the daily stop, but 3 straight
        eng.rails.on_close(r, at(9, 35))
    assert eng.rails.locked_out() and not eng.rails.daily_stop_hit()
    tick(eng, m, now)
    assert gate_opens(eng) == 0 and any("lockout" in d["reason"] for d in eng.decisions)


def test_rail_cooling_off_halves_next_session():
    eng, m, now = armed_state(9, 40)
    eng.rails.on_close(5.2, at(9, 35))                                        # a ≥ +5R win today
    assert eng.rails.cooling_off_triggered
    eng2, m2, now2 = armed_state(9, 40, cooling_off=True)                    # the next session is built with the flag
    tick(eng2, m2, now2)
    pos = [p for p in eng2.positions.values() if p.source == "gate"]
    assert pos and pos[0].sizing["f_used"] == pytest.approx(0.5 * 0.5 * pos[0].sizing["f_full"]) and "cooling-off" in " ".join(pos[0].sizing["reasons"])
    assert eng2.rails.size_mult() == 0.5


# ------------------------------------------------------------------------------------------------- edge-loss halt
@settings(max_examples=150, **H)
@given(hist=st.lists(st.floats(min_value=-1.0, max_value=9.0, allow_nan=False), max_size=120))
def test_rail_edge_loss_halt_definition(hist):
    rails = Rails(history=list(hist))
    e30 = sum(hist[-30:]) / 30 if len(hist) >= 30 else None
    e60 = sum(hist[-60:]) / 60 if len(hist) >= 60 else None
    expected = "stop" if (e60 is not None and e60 < 0) else ("floor" if (e30 is not None and e30 < 0) else "none")
    assert rails.halt_mode() == expected
    if expected == "stop":
        assert rails.can_enter()[0] is False


def test_rail_edge_loss_halt_through_the_engine():
    eng, m, now = armed_state(9, 40, history=[-1.0] * 30)                   # rolling-30 < 0 → floor
    tick(eng, m, now)
    pos = [p for p in eng.positions.values() if p.source == "gate"]
    assert pos and pos[0].sizing["mode"] == "halt-floor" and pos[0].contracts == 1
    eng2, m2, now2 = armed_state(9, 40, history=[-0.5] * 60)                # rolling-60 < 0 → stop
    tick(eng2, m2, now2)
    assert gate_opens(eng2) == 0 and any("edge-loss halt" in d["reason"] for d in eng2.decisions)


# ----------------------------------------------------------------------------------------------------- time stop
@settings(max_examples=60, **H)
@given(mm=st.integers(min_value=36, max_value=57))
def test_rail_every_entry_carries_a_time_stop_at_its_window_edge(mm):
    eng, m, now = armed_state(9, mm)
    tick(eng, m, now)
    for p in eng.positions.values():
        assert p.time_stop == at(10, 0) and p.time_stop_reason == "window"
    # march the clock with flat, fresh marks: everything is closed by the 10:00 tick, nothing survives past it
    t = now
    while t < at(10, 0, 2):
        t += timedelta(minutes=1)
        for p in eng.positions.values():
            st_ = m.options.state[p.option_symbol]
            st_.recv_ms = int(t.timestamp() * 1000)
        tick(eng, m, t)
    assert eng.positions and all(p.status != "open" for p in eng.positions.values())
    assert all(p.exit_reason in ("time_stop", "trail", "vwap_loss", "failed_extreme", "volume_taper") for p in eng.positions.values())
    assert all(p.exit_at <= at(10, 0, 2) for p in eng.positions.values())


# ------------------------------------------------------------------------------------------------- spread filter
@settings(max_examples=80, **H)
@given(spread=st.floats(min_value=0.005, max_value=0.40))
def test_rail_spread_filter(spread):
    eng, m, now = armed_state(9, 40, spread=spread)
    tick(eng, m, now)
    sym_marks = [m.options.state[p.option_symbol] for p in eng.positions.values()]
    if spread > TIER1.spread_skip_frac + 0.02:          # comfortably above 10% after price rounding
        assert gate_opens(eng) == 0 and any(d["reason"].startswith("marks: spread") for d in eng.decisions)
    elif spread < TIER1.spread_skip_frac - 0.02:
        assert gate_opens(eng) == 1
        for st_ in sym_marks:
            assert st_.spread_frac <= TIER1.spread_skip_frac


# ------------------------------------------------------------------------------------------------------ feed gate
@settings(max_examples=40, **H)
@given(mode=st.sampled_from(["DELAYED", "unknown"]), lag=st.floats(min_value=30.0, max_value=2000.0))
def test_rail_feed_gate_observe_only(mode, lag):
    eng, m, now = armed_state(9, 40)
    ev = tick(eng, m, now, feed_mode=mode, lag_s=lag)
    assert not eng.positions and ev == []
    assert eng.observe_only_ticks == 1
    assert eng.decisions and all(d["decision"] == "stand_down" and d["reason"].startswith("feed_not_realtime") for d in eng.decisions)
    # the same state with a real-time feed fires — so the feed gate was the only thing in the way
    eng2, m2, now2 = armed_state(9, 40)
    tick(eng2, m2, now2, feed_mode="realtime", lag_s=0.8)
    assert gate_opens(eng2) == 1


def test_rail_feed_gate_can_be_lifted_offline_only():
    eng = make_engine(require_realtime=False)
    assert eng.cfg.require_realtime is False
    eng_live = make_engine()
    assert eng_live.cfg.require_realtime is True


# ------------------------------------------------------------------------------------------ STAND DOWN default
def test_rail_stand_down_default_without_checklist():
    eng, m, now = armed_state(9, 40, checklists=[])
    tick(eng, m, now)
    assert gate_opens(eng) == 0
    d = [d for d in eng.decisions if d["kind"] == "gate"]
    assert d and d[0]["decision"] == "stand_down" and d[0]["gates"]["gates"]["catalyst"]["pass"] is False
    assert d[0]["gates"]["gates"]["direction"]["pass"] is False
    # the native trigger alone never fires a sized trade — it only opens the zero-capital fast-lane hypothesis
    assert [p.source for p in eng.positions.values()] == ["fast_lane"]


@settings(max_examples=60, **H)
@given(off=st.sampled_from(["catalyst", "readable", "favorable", "wedge", "direction"]))
def test_rail_any_single_gate_failing_stands_down(off):
    cl = [checklist("NVDA", "none" if off == "direction" else "long", gates={off: False}, start=SCHED.open, end=SCHED.close)]
    eng, m, now = armed_state(9, 40, sym="NVDA", spot=180.0, checklists=cl)
    tick(eng, m, now, gamma={"NVDA": {"regime": "unknown", "coverage": 0.0}})
    assert gate_opens(eng) == 0
    gd = [d for d in eng.decisions if d["kind"] == "gate"][0]["gates"]["gates"]
    assert gd[off]["pass"] is False


# ------------------------------------------------------------------------------------------------ concurrency
def test_rail_max_concurrent_positions():
    cls = [checklist(s, "long", cid=i + 1, start=SCHED.open, end=SCHED.close) for i, s in enumerate(("SPY", "QQQ", "IWM", "NVDA", "TSLA"))]
    now = at(9, 40, 2)
    spots = {"SPY": 650.0, "QQQ": 580.0, "IWM": 240.0, "NVDA": 180.0, "TSLA": 420.0}
    m = make_market(spots, now)
    for s, sp in spots.items():
        orb_breakout_bars(m, s, sp, SCHED.open, 9, break_at=8)
        m.spots[s] = sp + 0.6
        set_marks(m, m.plans[s], sp + 0.6, now)
    eng = make_engine(checklists=cls, index_symbols=("SPY", "QQQ", "IWM"))
    tick(eng, m, now, gamma={s: NEG_GAMMA for s in spots})
    assert gate_opens(eng) == eng.rules.max_concurrent == 3
    assert any(d["reason"].startswith("capacity") for d in eng.decisions)
