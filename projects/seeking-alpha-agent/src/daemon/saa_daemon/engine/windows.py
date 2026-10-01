"""Window scheduler and theta clock (plan §3 Tier 1 clock rules; corpus mode-b-intraday-clock-theta-schedule).

For one trade date the schedule holds:
* the regular windows — open 09:30–10:00 (entries until 09:58; on a data day — a pre-market release such as CPI/NFP at
  08:30 — entries start 09:35), mid 10:00–11:30 (no fresh entries except against a named scheduled trigger, i.e. an
  event window), the Tier 1 dead zone 11:30–13:30, afternoon 13:30–15:00 (event windows only), last hour 15:00–15:55
  (entries until 15:40, only on a negative-gamma read);
* one event window per in-session scheduled release at T: blackout [T − 15 min, T] (never own through the release),
  entries [T + 5, T + 15], time stop T + 55; FOMC adds the press-conference window at T + 30 (entries 15 min, stop + 60);
* every window's `stop` is the time stop for positions entered in it; `time_stop_for()` also pulls it in to the next
  blackout start and to close − 5 min (0DTE must be out before the bell).

Theta clock: an untouched ATM 0DTE bought at the open is worth √(time left / 6.5 h) of its opening value — 0.920 at
10:30, 0.734 at 12:30, 0.620 at 13:30, 0.392 at 15:00, 0 at the close. Reported on every decision so the ledger can
price the cost of waiting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from ..clock import Schedule, et, et_dt
from .rules import Rules, parse_hhmm
from .tier1 import TIER1, Tier1


@dataclass(frozen=True)
class Window:
    kind: str                 # open | mid | afternoon | last_hour | event | presser
    name: str
    start: datetime
    entry_until: datetime     # entries allowed while start <= now < entry_until (and not blacked out / dead zone)
    stop: datetime            # time stop for positions opened in this window
    allows_entry: bool
    requires_negative_gamma: bool = False
    event: str | None = None

    def contains(self, now: datetime) -> bool:
        return self.start <= now < self.stop

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "start": et(self.start).strftime("%H:%M"), "entry_until": et(self.entry_until).strftime("%H:%M"),
                "stop": et(self.stop).strftime("%H:%M"), "allows_entry": self.allows_entry,
                "requires_negative_gamma": self.requires_negative_gamma, "event": self.event}


@dataclass(frozen=True)
class Blackout:
    start: datetime
    end: datetime
    event: str

    def contains(self, now: datetime) -> bool:
        return self.start <= now < self.end


def _parse_event_time(ev: dict[str, Any]) -> time | None:
    t = ev.get("time_et") or ev.get("time")
    if not t:
        return None
    try:
        return parse_hhmm(str(t)[:5])
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class WindowSchedule:
    trade_date: date
    windows: tuple[Window, ...]
    blackouts: tuple[Blackout, ...]
    open: datetime
    close: datetime
    data_day: bool
    events: tuple[str, ...]

    @classmethod
    def build(cls, d: date, rules: Rules, econ_events: Iterable[dict[str, Any]] | None = None, *, tier1: Tier1 = TIER1) -> "WindowSchedule":
        sched = Schedule.for_date(d)
        w = rules.windows
        last_exit = sched.close - timedelta(minutes=tier1.last_entry_before_close_minutes)

        def at(hhmm: str) -> datetime:
            return min(et_dt(d, parse_hhmm(hhmm)), last_exit)

        events = [e for e in (econ_events or []) if isinstance(e, dict)]
        times: list[tuple[str, time]] = []
        for e in events:
            t = _parse_event_time(e)
            if t is not None:
                times.append((str(e.get("event") or "release"), t))
        data_day = any(t < time(9, 30) for _, t in times)

        windows: list[Window] = []
        o = w["open"]
        if data_day:
            # a pre-market release (CPI/NFP 08:30): enter 09:35–09:50 per corpus PP3 → the open window starts at 09:35 for entries
            windows.append(Window("open", f"open (data day: entries from {o['data_day_entry_from']})", et_dt(d, parse_hhmm(o["data_day_entry_from"])),
                                  at(o["entry_until"]), at(o["stop"]), True))
        else:
            windows.append(Window("open", "open", et_dt(d, parse_hhmm(o["start"])), at(o["entry_until"]), at(o["stop"]), True))
        m = w["mid"]
        windows.append(Window("mid", "mid (named trigger only)", et_dt(d, parse_hhmm(m["start"])), et_dt(d, parse_hhmm(m["start"])), at(m["stop"]), False))
        a = w["afternoon"]
        windows.append(Window("afternoon", "afternoon (event windows only)", et_dt(d, parse_hhmm(a["start"])), et_dt(d, parse_hhmm(a["start"])), at(a["stop"]), False))
        lh = w["last_hour"]
        lh_start = min(et_dt(d, parse_hhmm(lh["start"])), sched.close - timedelta(minutes=60))
        windows.append(Window("last_hour", "last hour (negative gamma only)", lh_start, min(at(lh["entry_until"]), last_exit), at(lh["stop"]), True,
                              requires_negative_gamma=bool(lh.get("require_negative_gamma", True))))

        blackouts: list[Blackout] = []
        ev = w["event"]
        for name, t in sorted(times, key=lambda x: x[1]):
            T = et_dt(d, t)
            if not (sched.open <= T < sched.close):
                continue        # pre-market / after-close releases shape the open window (data day) but need no blackout
            blackouts.append(Blackout(T - timedelta(minutes=tier1.release_blackout_minutes), T, name))
            start = T + timedelta(minutes=int(ev["entry_after_min"]))
            until = T + timedelta(minutes=int(ev["entry_until_min"]))
            stop = min(T + timedelta(minutes=int(ev["stop_after_min"])), last_exit)
            if start < last_exit:
                windows.append(Window("event", f"event:{name}", start, min(until, last_exit), stop, True, event=name))
            if "fomc" in name.lower():
                ps = T + timedelta(minutes=int(ev["presser_offset_min"]))
                pu = ps + timedelta(minutes=int(ev["presser_entry_min"]))
                pstop = min(ps + timedelta(minutes=int(ev["presser_stop_min"])), last_exit)
                if ps < last_exit:
                    windows.append(Window("presser", f"presser:{name}", ps, min(pu, last_exit), pstop, True, event=name))
        # drop windows entirely after the (early) close
        windows = [x for x in windows if x.start < sched.close]
        windows.sort(key=lambda x: (x.start, x.kind))
        return cls(d, tuple(windows), tuple(blackouts), sched.open, sched.close, data_day, tuple(n for n, _ in times))

    # --------------------------------------------------------------------------------------------------- queries
    def in_dead_zone(self, now: datetime, tier1: Tier1 = TIER1) -> bool:
        t = et(now).time()
        return tier1.no_entry_start <= t < tier1.no_entry_end

    def blackout_at(self, now: datetime) -> Blackout | None:
        for b in self.blackouts:
            if b.contains(now):
                return b
        return None

    def next_blackout_after(self, now: datetime) -> Blackout | None:
        later = [b for b in self.blackouts if b.start > now]
        return min(later, key=lambda b: b.start) if later else None

    def regular_window(self, now: datetime) -> Window | None:
        """The regular (non-event) window containing `now`."""
        for w in self.windows:
            if w.kind in ("open", "mid", "afternoon", "last_hour") and w.contains(now):
                return w
        return None

    def event_window(self, now: datetime) -> Window | None:
        """The event/presser window containing `now`; one still accepting entries wins over one that is only running."""
        hit: Window | None = None
        for w in self.windows:
            if w.kind in ("event", "presser") and w.start <= now < w.stop:
                if w.start <= now < w.entry_until:
                    return w
                hit = hit or w
        return hit

    def entry_window(self, now: datetime, *, tier1: Tier1 = TIER1) -> tuple[Window | None, str]:
        """(window that allows a fresh entry now, reason when none). Event windows win over the regular window."""
        if now < self.open:
            return None, "pre-open"
        last_exit = self.close - timedelta(minutes=tier1.last_entry_before_close_minutes)
        if now >= last_exit:
            return None, "no entries inside the last 5 minutes"
        b = self.blackout_at(now)
        if b is not None:
            return None, f"release blackout ({b.event} at {et(b.end):%H:%M})"
        ev = self.event_window(now)
        if ev is not None and ev.start <= now < ev.entry_until:
            return ev, ""
        if self.in_dead_zone(now, tier1):
            return None, "Tier 1: no fresh entries 11:30–13:30"
        w = self.regular_window(now)
        if w is None:
            return None, "no window"
        if not w.allows_entry:
            return None, f"{w.kind}: fresh entries only against a named scheduled trigger"
        if not (w.start <= now < w.entry_until):
            return None, f"{w.kind}: entries closed at {et(w.entry_until):%H:%M}"
        return w, ""

    def time_stop_for(self, window: Window, now: datetime, *, tier1: Tier1 = TIER1) -> datetime:
        """Window edge, pulled in to the next release blackout and to close − 5 min."""
        stop = min(window.stop, self.close - timedelta(minutes=tier1.last_entry_before_close_minutes))
        nb = self.next_blackout_after(now)
        if nb is not None and nb.start < stop:
            stop = nb.start
        return stop

    def as_dict(self) -> dict[str, Any]:
        return {"trade_date": self.trade_date.isoformat(), "data_day": self.data_day, "events": list(self.events),
                "windows": [w.as_dict() for w in self.windows],
                "blackouts": [{"start": et(b.start).strftime("%H:%M"), "end": et(b.end).strftime("%H:%M"), "event": b.event} for b in self.blackouts]}


# ------------------------------------------------------------------------------------------------------- theta clock
def theta_clock(now: datetime, open_: datetime, close: datetime) -> float:
    """Value of an untouched ATM 0DTE bought at the open as a fraction of its opening value (√time model)."""
    total = (close - open_).total_seconds()
    left = (close - now).total_seconds()
    if total <= 0 or left <= 0:
        return 0.0
    if left >= total:
        return 1.0
    return round(math.sqrt(left / total), 3)


def theta_cost_of_wait(entry: datetime, exit_: datetime, open_: datetime, close: datetime) -> float:
    """Fraction of the opening value lost to the clock between two instants (positive = cost)."""
    return round(theta_clock(entry, open_, close) - theta_clock(exit_, open_, close), 3)
