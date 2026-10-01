"""M3 acceptance: "a replay of ≥5 recorded sessions through the engine produces a deterministic trade list (same input →
byte-identical output)". Five synthetic sessions are written in the daemon's own recording format (Meta / Plan / market
events / OptMarks / Tick) from seeded random walks, each replayed twice; plus a pre-M3 recording (market events only,
like the real 2026-09-28 file) to prove the synthesized-tick path, and — when the real recording fixture is present —
the real session itself."""
from __future__ import annotations

import gzip
import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from engine_harness import D, SCHED, at, checklist, make_plan
from saa_daemon.engine.replay import replay_engine
from saa_daemon.engine.rules import DEFAULT_PARAMS
from saa_daemon.market import plan_to_record

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SYMS = {"SPY": 650.0, "QQQ": 580.0, "NVDA": 180.0}


def write_recording(path: Path, seed: int, *, minutes: int = 95, with_engine_records: bool = True, lag_ms: int = 0) -> None:
    """A session from 09:25 to 09:30 + `minutes`, three symbols, 1-minute bars with three forming updates, option marks
    for the planned strikes every minute, Meta/Plan/OptMarks/Tick records exactly where the live daemon writes them."""
    rng = random.Random(seed)
    plans = {s: make_plan(s, spot, n_side=10) for s, spot in SYMS.items()}
    prices = dict(SYMS)
    drift = {s: rng.choice([-1, 1]) * rng.uniform(0.0002, 0.0012) for s in SYMS}
    out: list[str] = []

    def w(rec: dict) -> None:
        out.append(json.dumps(rec, separators=(",", ":"), default=str))

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    t0 = at(9, 25, 0)
    if with_engine_records:
        for s in sorted(SYMS):
            w({"kind": "Plan", **plan_to_record(plans[s]), "recv_ms": ms(t0)})
        cls = [checklist("SPY", "long", start=SCHED.open, end=SCHED.open + timedelta(minutes=30), cid=1),
               checklist("NVDA", rng.choice(["long", "short"]), start=SCHED.open, end=SCHED.open + timedelta(minutes=30), cid=2)]
        w({"kind": "Meta", "trade_date": D.isoformat(), "account": 1000.0, "kelly_k": 0.5, "index_symbols": ["SPY", "QQQ"], "universe": sorted(SYMS),
           "rules": {"version": 2, "params": DEFAULT_PARAMS, "evidence": "test"}, "checklists": cls, "econ": [{"event": "Dallas Fed", "time_et": "10:30"}],
           "history_r": [], "cooling_off": False, "require_realtime": True, "version": "0.3.0-test", "run_id": f"synthetic-{seed}", "recv_ms": ms(t0)})

    def marks(sym: str, spot: float, now: datetime) -> dict:
        mk = {}
        for e in plans[sym].expirations:
            for sp in e.strikes:
                if abs(sp.strike - spot) / spot <= 0.015:
                    for osym, right in ((sp.call, "C"), (sp.put, "P")):
                        m = abs(sp.strike - spot) / spot
                        intrinsic = max(0.0, spot - sp.strike) if right == "C" else max(0.0, sp.strike - spot)
                        mid = max(0.05, round(intrinsic + 1.6 * math.exp(-m * 60) * (1 + 0.1 * math.sin(now.minute)), 2))
                        half = round(max(0.01, mid * 0.02), 2)
                        mk[osym] = [round(mid - half, 2), round(mid + half, 2), 0.18 + 0.4 * m, 0.5 if right == "C" else -0.5, 0.03, -0.1, 1000.0, 50.0, ms(now), ms(now)]
        return {"symbol": sym, "spot": round(spot, 4), "marks": dict(sorted(mk.items()))}

    gamma = {s: {"regime": "negative", "coverage": 1.0, "flip": SYMS[s] + 1, "call_wall": SYMS[s] + 5, "put_wall": SYMS[s] - 5} for s in SYMS}
    vix = {"vix": 14.9, "vix1d": 12.5, "vix9d": 12.8, "vix3m": 17.9, "shape": "contango"}
    bar_open = dict(prices)
    for minute in range(-5, minutes):
        bar_start = SCHED.open + timedelta(minutes=minute)
        in_session = minute >= 0
        for offs, frac in ((5, 0.1), (30, 0.5), (55, 0.9)):
            now = bar_start + timedelta(seconds=offs)
            for s in sorted(SYMS):
                step = prices[s] * (drift[s] * frac * 0.3 + rng.gauss(0, 0.0004))
                if s == "SPY" and minute == 6:
                    step = abs(step) + prices[s] * 0.0012        # SPY breaks its opening range on the 7th bar …
                px = round(prices[s] + step, 2)
                prices[s] = px
                vol = (2600.0 if (s == "SPY" and minute == 6) else 1000.0) * frac
                if in_session:
                    o = bar_open.setdefault((s, minute), px)
                    w({"symbol": s, "time_ms": ms(bar_start), "open": o, "high": max(o, px) + 0.05, "low": min(o, px) - 0.05, "close": px, "volume": vol,
                       "vwap": round((o + px) / 2, 4), "count": int(40 * frac), "kind": "Candle", "recv_ms": ms(now)})
                w({"symbol": s, "time_ms": ms(now), "bid": round(px - 0.01, 2), "ask": round(px + 0.01, 2), "bid_size": 100.0, "ask_size": 100.0, "kind": "Quote", "recv_ms": ms(now)})
                w({"symbol": s, "time_ms": ms(now) - lag_ms, "price": px, "size": 100.0, "day_volume": 1e6 * (minute + 6), "kind": "Trade", "recv_ms": ms(now)})
        # the engine tick 2 s into the NEXT minute: digests then Tick
        if with_engine_records:
            tick_at = bar_start + timedelta(minutes=1, seconds=2)
            for s in sorted(SYMS):
                w({"kind": "OptMarks", **marks(s, prices[s], tick_at), "recv_ms": ms(tick_at)})
            w({"kind": "Tick", "feed_mode": "realtime" if lag_ms < 30_000 else "DELAYED", "lag_s": lag_ms / 1000.0, "vix": vix, "gamma": gamma,
               "universe": sorted(SYMS), "final": minute == minutes - 1, "recv_ms": ms(tick_at)})
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_replay_is_byte_identical_per_recording(tmp_path: Path, seed: int):
    rec = tmp_path / f"synthetic-{seed}.jsonl"
    write_recording(rec, seed)
    a, b = replay_engine(rec), replay_engine(rec)
    assert a.canonical == b.canonical and a.sha256 == b.sha256
    assert not a.synthesized_ticks and a.ticks == 100 and a.feed_modes == {"realtime": 100}
    assert a.meta["rules"]["version"] == 2 and a.engine.rules.version == 2 and len(a.engine.checklists) == 2
    s = a.summary()
    assert s["decisions"] >= 3 and s["fast_lane_opens"] >= 1                       # the SPY opening-range break is in every seed
    # everything opened is closed by the final tick, and no position lived through the 10:15–10:30 blackout
    for p in a.engine.positions.values():
        assert p.status in ("closed", "void")
        if p.status == "closed":
            assert p.exit_at <= at(10, 15, 2) or p.opened_at >= at(10, 30)


def test_five_recordings_differ_but_each_is_stable(tmp_path: Path):
    shas = {}
    for seed in (11, 12, 13, 14, 15):
        rec = tmp_path / f"s{seed}.jsonl"
        write_recording(rec, seed)
        r1, r2 = replay_engine(rec), replay_engine(rec)
        assert r1.canonical == r2.canonical
        shas[seed] = r1.sha256
    assert len(set(shas.values())) == 5                                                 # different sessions → different ledgers
    # a byte flipped in the input changes the output (the hash really covers the inputs that matter)
    rec = tmp_path / "s11.jsonl"
    lines = rec.read_text(encoding="utf-8").splitlines()
    ticks = [i for i, ln in enumerate(lines) if '"kind":"Tick"' in ln and '"final":false' in ln]
    idx = ticks[12]                                                                       # an in-session tick (09:38:02), not a pre-open one
    lines[idx] = lines[idx].replace('"feed_mode":"realtime"', '"feed_mode":"DELAYED"')
    rec.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert replay_engine(rec).sha256 != shas[11]


def test_pre_m3_recording_replays_with_synthesized_ticks(tmp_path: Path):
    """No Meta / Tick / OptMarks (the 2026-09-28 real file's shape): ticks are synthesized per receipt minute, the feed
    mode comes from the recording's own trade stamps, and without marks nothing can open — deterministically."""
    rec = tmp_path / "pre-m3.jsonl"
    write_recording(rec, 7, minutes=40, with_engine_records=False, lag_ms=901_000)
    a, b = replay_engine(rec, require_realtime=False), replay_engine(rec, require_realtime=False)
    assert a.canonical == b.canonical and a.synthesized_ticks and a.ticks >= 40
    assert set(a.feed_modes) == {"DELAYED"} and a.meta["synthetic"] is True and a.meta["index_symbols"] == ["SPY", "QQQ"]
    assert a.summary()["fired"] == 0 and a.summary()["ledger_rows"] == 0
    reasons = {d["reason"].split(":")[0] for d in a.engine.decisions}
    assert reasons <= {"gates", "marks", "clock"} and a.engine.decisions          # evaluated, stood down (no checklist, no marks)
    # the same file with the live gate on: observe-only because the recording itself is 15 minutes delayed
    c = replay_engine(rec)
    assert c.engine.observe_only_ticks == c.engine.entry_ticks > 0 and all(d["reason"].startswith("feed_not_realtime") for d in c.engine.decisions)


REAL = FIXTURES / "recording-2026-09-28-SPY-QQQ-IWM.jsonl.gz"


@pytest.mark.skipif(not REAL.is_file(), reason="real 2026-09-28 recording fixture not present")
def test_real_2026_09_28_recording_replays_deterministically(tmp_path: Path):
    rec = tmp_path / "real.jsonl"
    rec.write_bytes(gzip.decompress(REAL.read_bytes()))
    a, b = replay_engine(rec, require_realtime=False, trade_date=date(2026, 9, 28)), replay_engine(rec, require_realtime=False, trade_date=date(2026, 9, 28))
    assert a.canonical == b.canonical and a.synthesized_ticks
    assert a.feed_modes.get("DELAYED", 0) > 0.9 * a.ticks                       # the production feed was 15 minutes delayed that day
    assert a.summary()["fired"] == 0                                              # no checklists / marks in a pre-M3 file → stand-downs only
    gated = replay_engine(rec, trade_date=date(2026, 9, 28))
    assert gated.engine.observe_only_ticks == gated.engine.entry_ticks > 0
    assert a.records == b.records >= 10_000
