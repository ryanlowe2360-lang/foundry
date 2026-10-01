"""Tier 1 rails as state: the edge-loss halt, the daily −3R stop, the 3-loss lockout, cooling-off, and the running
R-count for the day. Everything here is a pure function of the closed-trade history handed in and the closes reported
through `on_close()`. Fast-lane hypotheses (zero capital) never enter this history — only gate-fired trades do.

Edge-loss halt (replaces the drawdown halt): rolling 30-trade expectancy < 0 → size at the floor; rolling 60-trade
expectancy < 0 → no new entries pending a written review. Both need the full sample (30 / 60 closed trades) to fire;
below that the Kelly shrinkage already keeps size near the floor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .tier1 import TIER1, Tier1


def expectancy(rs: Iterable[float]) -> float | None:
    xs = [float(r) for r in rs]
    return (sum(xs) / len(xs)) if xs else None


@dataclass
class DayState:
    closed: int = 0
    realized_r: float = 0.0          # Σ r_result of today's closed gate trades (each trade risks 1R)
    consecutive_losses: int = 0
    max_win_r: float = 0.0
    wins: int = 0
    losses: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"closed": self.closed, "realized_r": round(self.realized_r, 4), "consecutive_losses": self.consecutive_losses,
                "max_win_r": round(self.max_win_r, 4), "wins": self.wins, "losses": self.losses}


@dataclass
class Rails:
    tier1: Tier1 = TIER1
    history: list[float] = field(default_factory=list)   # closed gate-trade results in R, oldest first (prior sessions + today)
    cooling_off_today: bool = False                      # set by the daemon from the previous session's trigger
    day: DayState = field(default_factory=DayState)
    cooling_off_triggered: bool = False                  # today triggered a cooling-off for the NEXT session
    events: list[dict[str, Any]] = field(default_factory=list)

    # ----------------------------------------------------------------------------------------------- edge-loss halt
    def rolling(self, n: int) -> float | None:
        if len(self.history) < n:
            return None
        return expectancy(self.history[-n:])

    def halt_mode(self) -> str:
        e60 = self.rolling(self.tier1.halt_stop_window)
        if e60 is not None and e60 < 0:
            return "stop"
        e30 = self.rolling(self.tier1.halt_floor_window)
        if e30 is not None and e30 < 0:
            return "floor"
        return "none"

    # ------------------------------------------------------------------------------------------------- day rails
    def daily_stop_hit(self) -> bool:
        return self.day.realized_r <= self.tier1.daily_stop_r

    def locked_out(self) -> bool:
        return self.day.consecutive_losses >= self.tier1.consecutive_loss_lockout

    def can_enter(self) -> tuple[bool, str]:
        """Day/edge rails only; the clock rails live in WindowSchedule.entry_window()."""
        if self.halt_mode() == "stop":
            return False, f"edge-loss halt: rolling-{self.tier1.halt_stop_window} expectancy < 0 (review required)"
        if self.daily_stop_hit():
            return False, f"daily stop: {self.day.realized_r:+.2f}R ≤ {self.tier1.daily_stop_r:+.0f}R"
        if self.locked_out():
            return False, f"lockout: {self.day.consecutive_losses} straight losses today"
        return True, ""

    def size_mult(self) -> float:
        return self.tier1.cooling_off_size_mult if self.cooling_off_today else 1.0

    def on_close(self, r_result: float, now: datetime, *, counts: bool = True) -> list[str]:
        """Record a closed trade. `counts=False` for fast-lane hypotheses (journal only, no rail effect)."""
        notes: list[str] = []
        if not counts:
            return notes
        r = float(r_result)
        self.history.append(r)
        d = self.day
        d.closed += 1
        d.realized_r += r
        if r > 0:
            d.wins += 1
            d.consecutive_losses = 0
            d.max_win_r = max(d.max_win_r, r)
        else:
            d.losses += 1
            d.consecutive_losses += 1
        if self.daily_stop_hit():
            notes.append("daily stop reached")
        if self.locked_out():
            notes.append("3-loss lockout")
        if (r >= self.tier1.cooling_off_win_r or d.realized_r >= self.tier1.cooling_off_day_r) and not self.cooling_off_triggered:
            self.cooling_off_triggered = True
            notes.append("cooling-off: next session at half size")
        hm = self.halt_mode()
        if hm != "none":
            notes.append(f"edge-loss halt: {hm}")
        for n in notes:
            self.events.append({"at": now.isoformat(), "note": n, "r": round(r, 4)})
        return notes

    def state(self) -> dict[str, Any]:
        return {"halt_mode": self.halt_mode(), "n_history": len(self.history), "rolling_30": _r(self.rolling(30)), "rolling_60": _r(self.rolling(60)),
                "daily_stop_hit": self.daily_stop_hit(), "locked_out": self.locked_out(), "cooling_off_today": self.cooling_off_today,
                "cooling_off_triggered": self.cooling_off_triggered, "size_mult": self.size_mult(), "day": self.day.as_dict()}


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 4)
