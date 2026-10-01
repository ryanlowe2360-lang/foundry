"""Engine building blocks: Tier 2 rules loading (and Tier 1 refusal), the window schedule and theta clock, native bar
triggers, the six gates on constructed inputs, shadow-position exits (bank → trail → tighten, mechanism exits, time
stop, expiry, void), and the engine's canonical output being byte-stable."""
from __future__ import annotations

from collections import deque
from datetime import date, time, timedelta

import pytest

from engine_harness import (D, NEG_GAMMA, SCHED, VIX, at, candle, checklist, flat_bars, make_engine, make_market, orb_breakout_bars, set_marks,
                            tick, vwap_reclaim_bars)
from saa_daemon.bars import BarBook
from saa_daemon.clock import et, et_dt
from saa_daemon.engine.gates import ChainRead, evaluate_gates
from saa_daemon.engine.positions import Exit, Position
from saa_daemon.engine.rules import DEFAULT_PARAMS, Rules, RulesError
from saa_daemon.engine.tier1 import TIER1_KEYS_IN_RULES
from saa_daemon.engine.triggers import (continuation_signal, day_bars, failed_new_extreme, orb_signal, realized_vol_annualized, relative_volume,
                                        rvol_signal, session_vwap, volume_taper, vwap_signal)
from saa_daemon.engine.windows import WindowSchedule, theta_clock, theta_cost_of_wait


# ------------------------------------------------------------------------------------------------------- rules
def test_rules_defaults_and_v1_row_compat():
    r = Rules.default()
    assert r.version == 0 and r.bank_frac == 0.075 and r.trail["frac"] == 0.30 and r.max_concurrent == 3
    v1 = {"partial_bank_frac_of_account": 0.075, "trail": {"frac": 0.30, "tight_frac": 0.20, "tight_at_r": 3.0, "atr_ticks": 10},
          "spread_filter": {"flag": 0.05, "skip": 0.10}, "daily_stop_r": -3, "consecutive_loss_lockout": 3,
          "cooling_off": {"win_r": 5, "day_r": 8, "next_size_mult": 0.5}, "no_entry_et": ["11:30", "13:30"],
          "fast_lanes": {"orb": {"enabled": True, "opens_shadow": True, "entry_until_et": "10:30"}}}
    r1 = Rules.from_params(v1, 1)
    assert r1.version == 1
    assert set(r1.ignored) == {"spread_filter", "daily_stop_r", "consecutive_loss_lockout", "cooling_off", "no_entry_et"}   # Tier 1 keys refused
    assert all(k not in r1.params for k in TIER1_KEYS_IN_RULES)
    assert r1.lane("orb")["range_minutes"] == 5 and r1.lane("orb")["entry_until_et"] == "10:30"        # merged with defaults
    assert r1.lane("vwap")["min_bars"] == 15


def test_rules_validation_rejects_nonsense():
    with pytest.raises(RulesError):
        Rules.from_params({"partial_bank_frac_of_account": 1.5}, 9)
    with pytest.raises(RulesError):
        Rules.from_params({"trail": {"frac": 0.1, "tight_frac": 0.5}}, 9)          # tight must be ≤ frac
    with pytest.raises(RulesError):
        Rules.from_params({"windows": {"open": {"start": "10:00", "entry_until": "09:50"}}}, 9)
    with pytest.raises(RulesError):
        Rules.from_params({"shadow": {"last_entry_et": "25:99"}}, 9)
    assert Rules.from_row(None).version == 0 and Rules.from_row({"version": 3, "params": "junk"}).version == 0
    r = Rules.from_row({"version": 2, "params": DEFAULT_PARAMS, "evidence": "e", "effective_from": "2026-10-01"})
    assert r.version == 2 and r.effective_from == "2026-10-01" and r.summary()["lanes_on"] == ["continuation", "orb", "rvol", "vwap"]


# --------------------------------------------------------------------------------------------- windows / theta
def test_window_schedule_regular_day():
    ws = WindowSchedule.build(D, Rules.default())
    kinds = [w.kind for w in ws.windows]
    assert kinds == ["open", "mid", "afternoon", "last_hour"] and not ws.data_day and ws.blackouts == ()
    assert ws.entry_window(at(9, 29))[0] is None
    w, _ = ws.entry_window(at(9, 45))
    assert w.kind == "open" and ws.time_stop_for(w, at(9, 45)) == at(10, 0)
    assert ws.entry_window(at(9, 59))[0] is None                                   # entries close 09:58
    assert ws.entry_window(at(10, 30))[1].startswith("mid")
    assert ws.entry_window(at(12, 0))[1].startswith("Tier 1")
    assert ws.entry_window(at(14, 0))[1].startswith("afternoon")
    lh, _ = ws.entry_window(at(15, 10))
    assert lh.kind == "last_hour" and lh.requires_negative_gamma and ws.time_stop_for(lh, at(15, 10)) == at(15, 55)
    assert ws.entry_window(at(15, 41))[0] is None and ws.entry_window(at(15, 56))[0] is None


def test_window_schedule_events_and_data_day():
    econ = [{"event": "CPI", "time_et": "08:30"}, {"event": "FOMC decision", "time_et": "14:00"}, {"event": "Fed speaker", "time_et": "16:30"}]
    ws = WindowSchedule.build(D, Rules.default(), econ)
    assert ws.data_day and [b.event for b in ws.blackouts] == ["FOMC decision"]           # after-close events shape nothing
    assert ws.windows[0].kind == "open" and ws.windows[0].start == at(9, 35)              # data day: entries from 09:35
    assert ws.entry_window(at(9, 32))[0] is None and ws.entry_window(at(9, 36))[0].kind == "open"
    assert ws.entry_window(at(13, 50))[1].startswith("release blackout")
    ev, _ = ws.entry_window(at(14, 7))
    assert ev.kind == "event" and ev.event == "FOMC decision" and ev.entry_until == at(14, 15) and ev.stop == at(14, 55)
    assert ws.entry_window(at(14, 20))[0] is None                                          # between statement and presser
    pr, _ = ws.entry_window(at(14, 35))
    assert pr.kind == "presser" and pr.stop == at(15, 30)
    # a position opened at 13:35 (afternoon, fast lane) is cut at the blackout start
    w = ws.regular_window(at(13, 35))
    assert w.kind == "afternoon" and ws.time_stop_for(w, at(13, 35)) == at(13, 45)


def test_window_schedule_early_close():
    d = date(2026, 11, 27)                                                                # 13:00 close
    ws = WindowSchedule.build(d, Rules.default())
    assert ws.close == et_dt(d, time(13, 0))
    assert all(w.start < ws.close for w in ws.windows)
    lh = [w for w in ws.windows if w.kind == "last_hour"][0]
    assert lh.start == et_dt(d, time(12, 0)) and lh.stop == et_dt(d, time(12, 55)) and lh.entry_until <= et_dt(d, time(12, 55))


def test_theta_clock_matches_corpus_table():
    o, c = SCHED.open, SCHED.close
    assert theta_clock(at(9, 30), o, c) == 1.0
    assert theta_clock(at(10, 30), o, c) == 0.92
    assert theta_clock(at(12, 30), o, c) == 0.734
    assert theta_clock(at(13, 30), o, c) == 0.62
    assert theta_clock(at(15, 0), o, c) == 0.392
    assert theta_clock(at(16, 0), o, c) == 0.0 and theta_clock(at(9, 0), o, c) == 1.0
    assert theta_cost_of_wait(at(9, 30), at(10, 30), o, c) == 0.08


# ----------------------------------------------------------------------------------------------------- triggers
def test_native_triggers_on_shaped_bars():
    m = make_market({"SPY": 650.0}, at(9, 20))
    orb_breakout_bars(m, "SPY", 650.0, SCHED.open, 8, break_at=6)
    bars = day_bars(m.bars, "SPY", SCHED.open, at(9, 37, 2))
    assert len(bars) == 7 and et(bars[-1].start).time() == time(9, 36)
    s = orb_signal(bars, range_minutes=5, volume_mult=1.5)
    assert s and s.lane == "orb" and s.direction == "long" and s.price == 650.6
    assert orb_signal(bars[:6], range_minutes=5, volume_mult=1.5) is None                 # bar 09:35 is inside the range
    assert orb_signal(bars, range_minutes=5, volume_mult=5.0) is None                     # volume filter
    m2 = make_market({"SPY": 650.0}, at(9, 20))
    orb_breakout_bars(m2, "SPY", 650.0, SCHED.open, 8, break_at=6, direction="short")
    assert orb_signal(day_bars(m2.bars, "SPY", SCHED.open, at(9, 37, 2)), range_minutes=5, volume_mult=1.5).direction == "short"
    # VWAP reclaim
    m3 = make_market({"SPY": 650.0}, at(9, 20))
    vwap_reclaim_bars(m3, "SPY", 650.0, SCHED.open, 20)
    b3 = day_bars(m3.bars, "SPY", SCHED.open, at(9, 50, 2))
    v = vwap_signal(b3, min_bars=15)
    assert v and v.direction == "long" and session_vwap(b3) is not None
    assert vwap_signal(b3, min_bars=40) is None
    # continuation: a new session high after 10:00 on volume
    m4 = make_market({"SPY": 650.0}, at(9, 20))
    flat_bars(m4, "SPY", 650.0, SCHED.open, 40)
    m4.bars.on_candle(candle("SPY", SCHED.open + timedelta(minutes=40), 650.0, 651.4, 649.9, 651.3, 1500.0))
    b4 = day_bars(m4.bars, "SPY", SCHED.open, at(10, 11, 2))
    c = continuation_signal(b4, start_et=time(10, 0), lookback=10, volume_mult=1.2)
    assert c and c.direction == "long" and "new session high" in c.note
    assert continuation_signal(b4, start_et=time(10, 45), lookback=10, volume_mult=1.2) is None
    # rvol: inert without a baseline, fires with one
    assert relative_volume(b4, None) is None and rvol_signal(b4, None, mult=2.0) is None
    base = {i: 300.0 for i in range(60)}
    assert relative_volume(b4, base) == pytest.approx((40 * 1000 + 1500) / (41 * 300))
    assert rvol_signal(b4, base, mult=2.0).lane == "rvol"
    # realized vol / mechanism measures
    assert realized_vol_annualized(b4, 30) is not None and realized_vol_annualized(b4[:3], 60) is None
    assert failed_new_extreme(b4, "long") is False
    m4.bars.on_candle(candle("SPY", SCHED.open + timedelta(minutes=41), 651.3, 652.0, 650.9, 651.0, 900.0))   # pokes above 651.4 then closes below
    b5 = day_bars(m4.bars, "SPY", SCHED.open, at(10, 12, 2))
    assert failed_new_extreme(b5, "long") is True
    assert volume_taper(b5, b5[-2].start, n=3, frac=0.5) is False


# -------------------------------------------------------------------------------------------------------- gates
def test_gates_index_vs_single_name_inputs():
    rules = Rules.default()
    ws = WindowSchedule.build(D, rules)
    w = ws.regular_window(at(9, 40))
    m = make_market({"SPY": 650.0}, at(9, 20))
    orb_breakout_bars(m, "SPY", 650.0, SCHED.open, 8, break_at=6)
    bars = day_bars(m.bars, "SPY", SCHED.open, at(9, 37, 2))
    sig = [orb_signal(bars, range_minutes=5, volume_mult=1.5)]
    chain = ChainRead(1.0, 1.0, 0.20, 24)
    cl = checklist("SPY", "long")
    r = evaluate_gates(symbol="SPY", is_index=True, now=at(9, 37, 2), window=w, checklist=cl, gamma=NEG_GAMMA, vix=VIX, chain=chain, bars=bars, signals=sig, rules=rules)
    assert r.all_pass and r.direction == "long" and r.probability == 0.30 and r.gates["wedge"]["source"] == "brief"   # < 60 min of bars
    # index: positive gamma → unfavorable; unknown gamma → unreadable; no VIX → unreadable
    r2 = evaluate_gates(symbol="SPY", is_index=True, now=at(9, 37, 2), window=w, checklist=cl, gamma={**NEG_GAMMA, "regime": "positive"}, vix=VIX, chain=chain, bars=bars, signals=sig, rules=rules)
    assert r2.failed() == ["favorable"]
    r3 = evaluate_gates(symbol="SPY", is_index=True, now=at(9, 37, 2), window=w, checklist=cl, gamma=None, vix=None, chain=chain, bars=bars, signals=sig, rules=rules)
    assert set(r3.failed()) == {"readable", "favorable"}
    # single name: chain coverage + brief gates; two-sided accepted; trigger against the thesis fails gate 6
    cl_n = checklist("NVDA", "short")
    r4 = evaluate_gates(symbol="NVDA", is_index=False, now=at(9, 37, 2), window=w, checklist=cl_n, gamma={"regime": "unknown", "coverage": 0}, vix=VIX, chain=chain, bars=bars, signals=sig, rules=rules)
    assert r4.failed() == ["trigger"] and "against thesis" in r4.gates["trigger"]["note"]
    r5 = evaluate_gates(symbol="NVDA", is_index=False, now=at(9, 37, 2), window=w, checklist=checklist("NVDA", "two_sided"), gamma=None, vix=VIX, chain=chain, bars=bars, signals=sig, rules=rules)
    assert r5.all_pass and r5.direction == "two_sided"
    r6 = evaluate_gates(symbol="NVDA", is_index=False, now=at(9, 37, 2), window=w, checklist=cl_n, gamma=None, vix=VIX, chain=ChainRead(0.2, 0.2, None, 24), bars=bars, signals=[], rules=rules)
    assert "readable" in r6.failed() and "trigger" in r6.failed()
    # event window: the first post-release bar must close in the thesis direction
    ws_e = WindowSchedule.build(D, rules, [{"event": "FOMC decision", "time_et": "14:00"}])
    ev = ws_e.event_window(at(14, 6))
    bb = BarBook()
    bb.on_candle(candle("SPY", at(14, 5), 650.0, 650.9, 649.9, 650.8, 1000.0))
    up = day_bars(bb, "SPY", SCHED.open, at(14, 6, 2))
    r7 = evaluate_gates(symbol="SPY", is_index=True, now=at(14, 6, 2), window=ev, checklist=None, gamma=NEG_GAMMA, vix=VIX, chain=chain, bars=up, signals=[], rules=rules, release_passed=True)
    assert r7.gates["catalyst"]["pass"] and r7.gates["catalyst"]["source"] == "calendar" and not r7.gates["direction"]["pass"]   # no checklist → no direction
    r8 = evaluate_gates(symbol="SPY", is_index=True, now=at(14, 6, 2), window=ev, checklist=checklist("SPY", "long", window_kind="event"), gamma=NEG_GAMMA, vix=VIX, chain=chain, bars=up, signals=[], rules=rules, release_passed=True)
    assert r8.all_pass and r8.trigger.lane == "event"


# ---------------------------------------------------------------------------------------------------- positions
def _pos(contracts: int = 1, entry_ask: float = 1.00, account: float = 1000.0, is_index: bool = False) -> Position:
    p = Position(key="k", trade_date=D, symbol="NVDA", is_index=is_index, source="gate", lane="open", window="open", direction="long",
                 option_symbol=".NVDA260928C181", option_type="call", strike=181.0, expiration=D, dte=0, opened_at=at(9, 36, 2), entry_bar_time=at(9, 35),
                 entry_bid=round(entry_ask * 0.96, 2), entry_ask=entry_ask, entry_iv=0.3, entry_spot=180.0, sigma_d=0.02, contracts=contracts,
                 time_stop=at(10, 0), time_stop_reason="window", account=account, sizing={"mode": "test"})
    p.diffs = deque(maxlen=10)
    return p


def test_position_bank_then_trail_then_tighten():
    rules = Rules.default()
    p = _pos(contracts=2, entry_ask=1.00)                     # cost $200; bank trigger at +$75 open P&L
    assert p.on_mark(at(9, 37, 2), 1.30, 1.34, 180.5, rules) is None and not p.trail_on      # +$60 < $75
    assert p.on_mark(at(9, 38, 2), 1.40, 1.44, 180.6, rules) is None and p.trail_on            # +$80 ≥ $75 → bank
    # need = ceil(200 / 140) = 2 ≥ open 2 → cannot bank and keep a runner: whole position trails
    assert p.banked_contracts == 0 and p.open_contracts == 2 and p.events[-1]["event"] == "trail_on"
    p3 = _pos(contracts=3, entry_ask=1.00)                    # cost $300: bank 2 at 1.60 (ceil(300/160) = 2), run 1
    p3.on_mark(at(9, 37, 2), 1.60, 1.64, 180.6, rules)
    assert p3.trail_on and p3.banked_contracts == 2 and p3.open_contracts == 1 and p3.banked_proceeds == 320.0
    stop0 = p3.trail_stop
    assert stop0 == pytest.approx(1.60 - 0.30 * 0.60)        # 30% of the gain (the first mark has no ATR sample yet)
    for i, bid in enumerate((2.20, 2.90, 3.60)):
        assert p3.on_mark(at(9, 38 + i, 2), bid, bid + 0.06, 181.0 + i * 0.3, rules) is None
    assert not p3.trail_tight and p3.trail_stop == pytest.approx(3.60 - max(0.30 * 2.60, sum(p3.diffs) / len(p3.diffs)))
    p3.on_mark(at(9, 41, 2), 4.20, 4.26, 181.9, rules)       # +3.2R → tighten to 20% (latched)
    atr = sum(p3.diffs) / len(p3.diffs)
    assert p3.trail_tight and p3.trail_stop == pytest.approx(4.20 - max(0.20 * 3.20, atr)) and 0.6 < atr < 0.7
    ex = p3.on_mark(at(9, 42, 2), 3.40, 3.46, 181.5, rules)  # +2.4R: the 20% stop stays latched (30% would still hold) → exit
    assert ex and ex.reason == "trail"
    p3.close(at(9, 39, 2), ex)
    assert p3.status == "closed" and p3.proceeds == pytest.approx(320.0 + 340.0) and p3.r_result == pytest.approx((660.0 - 300.0) / 300.0)
    assert p3.mfe_r == pytest.approx(3.2) and p3.row()["banked_contracts"] == 2 and p3.row()["r_result"] == pytest.approx(1.2) and p3.row()["trail_tightened"]


def test_position_time_stop_expiry_void_and_full_loss():
    p = _pos()
    assert p.time_exit(at(9, 59, 2)) is None and p.time_exit(at(10, 0, 2)).reason == "time_stop"
    pe = _pos()
    pe.time_stop, pe.time_stop_reason = at(15, 55), "close"
    assert pe.time_exit(at(15, 55, 2)).reason == "expiry"                                 # 0DTE at the close cut-off
    pr = _pos()
    pr.time_stop_reason = "release"
    assert pr.time_exit(at(10, 0)).reason == "release_blackout"
    pf = _pos()
    pf.on_mark(at(9, 40), 0.01, 0.02, 179.0, Rules.default())
    pf.close(at(10, 0, 2), Exit("time_stop", "t"))
    assert pf.r_result == pytest.approx(-0.99) and pf.mae_r == pytest.approx(-0.99)
    pv = _pos()
    pv.void(at(10, 0), "no mark")
    assert pv.status == "void" and pv.row()["exit_reason"] == "void_no_marks"


def test_position_mechanism_exits():
    rules = Rules.default()
    m = make_market({"NVDA": 180.0}, at(9, 20))
    orb_breakout_bars(m, "NVDA", 180.0, SCHED.open, 8, break_at=6)
    p = _pos()
    p.entry_bar_time = SCHED.open + timedelta(minutes=6)
    bars = day_bars(m.bars, "NVDA", SCHED.open, at(9, 38, 2))
    assert p.on_bar_close(bars, rules) is None                                            # drifting up above VWAP: hold
    # a bar closing below VWAP → vwap_loss for a single name, not for an index
    m.bars.on_candle(candle("NVDA", SCHED.open + timedelta(minutes=8), 180.5, 180.6, 179.0, 179.1, 900.0))
    bars = day_bars(m.bars, "NVDA", SCHED.open, at(9, 39, 2))
    ex = p.on_bar_close(bars, rules)
    assert ex and ex.reason == "vwap_loss"
    pi = _pos(is_index=True)
    pi.entry_bar_time = SCHED.open + timedelta(minutes=6)
    ex_i = pi.on_bar_close(bars, rules)
    assert ex_i is None or ex_i.reason != "vwap_loss"
    # failed new extreme: pokes above the session high, closes below it
    m2 = make_market({"NVDA": 180.0}, at(9, 20))
    orb_breakout_bars(m2, "NVDA", 180.0, SCHED.open, 8, break_at=6)
    hi = max(b.high for b in day_bars(m2.bars, "NVDA", SCHED.open, at(9, 38, 2)))
    m2.bars.on_candle(candle("NVDA", SCHED.open + timedelta(minutes=8), hi - 0.02, hi + 0.3, hi - 0.1, hi - 0.05, 1200.0))
    p2 = _pos()
    p2.entry_bar_time = SCHED.open + timedelta(minutes=6)
    ex2 = p2.on_bar_close(day_bars(m2.bars, "NVDA", SCHED.open, at(9, 39, 2)), rules)
    assert ex2 and ex2.reason == "failed_extreme"
    # volume taper: three quiet bars after the burst
    m3 = make_market({"NVDA": 180.0}, at(9, 20))
    orb_breakout_bars(m3, "NVDA", 180.0, SCHED.open, 7, break_at=6)
    for i in range(3):
        m3.bars.on_candle(candle("NVDA", SCHED.open + timedelta(minutes=7 + i), 180.62, 180.66, 180.60, 180.63, 200.0))
    p3 = _pos()
    p3.entry_bar_time = SCHED.open + timedelta(minutes=6)
    ex3 = p3.on_bar_close(day_bars(m3.bars, "NVDA", SCHED.open, at(9, 41, 2)), rules)
    assert ex3 and ex3.reason == "volume_taper"


# ------------------------------------------------------------------------------------------- canonical output
def _run_twice():
    outs = []
    for _ in range(2):
        m = make_market({"SPY": 650.0, "NVDA": 180.0}, at(9, 20))
        eng = make_engine(checklists=[checklist("SPY", "long"), checklist("NVDA", "long", cid=2)])
        orb_breakout_bars(m, "SPY", 650.0, SCHED.open, 8, break_at=6)
        flat_bars(m, "NVDA", 180.0, SCHED.open, 8)
        t = at(9, 37, 2)
        m.spots["SPY"] = 650.6
        set_marks(m, m.plans["SPY"], 650.6, t)
        tick(eng, m, t)
        for i, bid in enumerate([1.3, 1.6, 2.0, 2.4, 1.5]):
            tt = at(9, 38 + i, 2)
            for p in eng.positions.values():
                st_ = m.options.state[p.option_symbol]
                st_.bid, st_.ask, st_.recv_ms = bid, round(bid * 1.04, 2), int(tt.timestamp() * 1000)
            tick(eng, m, tt)
        outs.append(eng)
    return outs


def test_engine_canonical_output_is_byte_stable_and_complete():
    a, b = _run_twice()
    assert a.canonical() == b.canonical()
    st = a.state()
    assert st["counts"]["fired"] == 1 and st["counts"]["fast_lane_opens"] == 1 and st["counts"]["closes"] == 2
    gate = [p for p in a.positions.values() if p.source == "gate"][0]
    assert gate.status == "closed" and gate.exit_reason == "trail" and gate.r_result > 0 and gate.trail_on
    assert st["rails"]["day"]["closed"] == 1 and st["rails"]["day"]["wins"] == 1          # fast-lane close does not count
    kinds = {(d["kind"], d["decision"]) for d in a.decisions}
    assert {("gate", "fire"), ("fast_lane", "open"), ("manage", "close"), ("gate", "stand_down")} <= kinds
    alert = [e for e in a.events if e["type"] == "alert"]
    assert len(alert) == 2 and alert[0]["text"].startswith("SHADOW open ▸ SPY long .SPY260928C655 ×1 @") and "SHADOW close ▸ SPY" in alert[1]["text"]
    row = gate.row()
    assert row["model_version"] == "dxlink-marks-v1" and row["source"] == "engine" and row["engine_source"] == "gate" and row["counts_for_rails"]
    assert row["entry_premium"] == gate.entry_ask and row["spread_frac"] == pytest.approx((gate.entry_ask - gate.entry_bid) / gate.entry_ask, abs=1e-6)


# ------------------------------------------------------------------------------------------------ feed-lag rule
def test_feed_lag_ignores_stale_regular_session_prints():
    """dxfeed's Trade event is the regular-session last sale: before the open it still carries yesterday's close, after the
    close it is frozen at 16:00. Neither says anything about the feed, so only trades stamped inside today's session count."""
    from saa_daemon.events import TradeEvt
    from saa_daemon.market import MarketState
    m = MarketState.new()
    m.underlyings.add("SPY")
    m.session_open = SCHED.open
    yesterday_close = at(16, 0, d=D - timedelta(days=3))                         # Friday's close, seen at 09:20 Monday
    m.on_event(TradeEvt("SPY", int(yesterday_close.timestamp() * 1000), 650.0, 100.0, 1e5), at(9, 20))
    assert m.feed_lag() == {"lag_s": None, "mode": "unknown", "n": 0}
    t = at(9, 31, 5)
    m.on_event(TradeEvt("SPY", int(t.timestamp() * 1000) - 800, 650.2, 100.0, 1e5), t)         # 0.8 s old: real-time
    assert m.feed_lag()["mode"] == "realtime" and m.feed_lag()["n"] == 1
    m2 = MarketState.new()
    m2.underlyings.add("SPY")
    m2.session_open = SCHED.open
    t2 = at(10, 0, 2)
    m2.on_event(TradeEvt("SPY", int(t2.timestamp() * 1000) - 901_000, 650.0, 100.0, 1e5), t2)   # 15 minutes old inside the session
    assert m2.feed_lag()["mode"] == "DELAYED" and m2.feed_lag()["lag_s"] == 901.0
    m3 = MarketState.new()                                                                      # no session_open set: old behaviour
    m3.underlyings.add("SPY")
    m3.on_event(TradeEvt("SPY", int(yesterday_close.timestamp() * 1000), 650.0, 100.0, 1e5), at(9, 20))
    assert m3.feed_lag()["mode"] == "DELAYED"
