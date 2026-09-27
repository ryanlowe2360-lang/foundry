"""Replay a recorded session (state/recordings/<run>.jsonl) through the bar book. Deterministic: the same
file always yields the same bars, which is the seed for M3's "replay of ≥5 recorded sessions" criterion.
Writes nothing to Supabase."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from .bars import BarBook
from .clock import Schedule, et
from .events import CandleEvt, from_record


def replay_bars(path: Path) -> dict[str, Any]:
    book = BarBook()
    n = 0
    first_ms = last_ms = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            recv = rec.pop("recv_ms", 0)
            e = from_record(rec)
            n += 1
            first_ms = first_ms or recv
            last_ms = recv or last_ms
            if isinstance(e, CandleEvt):
                book.on_candle(e)
    if last_ms:
        from .events import ms_to_dt
        book.complete_before(ms_to_dt(last_ms) + timedelta(minutes=1))
    out: dict[str, Any] = {"events": n, "symbols": {}}
    for sym in book.symbols():
        starts = sorted(t for (s, t) in book.bars if s == sym)
        if not starts:
            continue
        day = et(starts[0]).date()
        sched = Schedule.for_date(day)
        out["symbols"][sym] = {
            "bars": len(starts), "complete": book.count(sym, sched.open, sched.close),
            "gaps": [et(t).strftime("%H:%M") for t in book.gaps(sym, sched.open, min(sched.close, starts[-1] + timedelta(minutes=1)))][:20],
            "first": et(starts[0]).strftime("%H:%M"), "last": et(starts[-1]).strftime("%H:%M"),
            "last_close": book.last_close(sym),
        }
    return out
