"""The rules engine (M3): six-gate decision, window scheduler, theta clock, Tier 1 rails, fractional-Kelly sizing,
and the shadow ledger marked at bid (mid − half spread) from DXLink.

Design rules this package obeys:

* **Deterministic.** No wall clock, no randomness, no network. Every input arrives through `Engine.on_minute()`; the
  same sequence of inputs produces byte-identical decisions and ledger rows (`replay.canonical()`), which is how a
  recorded session can be replayed and audited.
* **Tier 1 is code, Tier 2 is data.** `tier1.TIER1` is a frozen dataclass — the survival rails cannot be changed by a
  `saa.rules` row, a setting, or Claude. `rules.Rules` carries the learned parameters from the versioned `saa.rules`
  table (the Friday review rewrites those, one change per week, ≥30 relevant trades).
* **STAND DOWN is the default.** A window with no checklist, an unreadable regime, an unknown direction, a missing
  trigger, a delayed feed, a stale mark or a wide spread records a decision row *and does nothing*.
* **No orders.** M3 opens shadow trades only; the ledger exists to measure p and W before M4 trades paper.
"""
from __future__ import annotations

from .engine import Engine, EngineConfig, EngineInputs
from .kelly import kelly_fraction, growth_per_trade, posterior, size_position
from .rules import Rules
from .tier1 import TIER1, Tier1

__all__ = ["Engine", "EngineConfig", "EngineInputs", "Rules", "TIER1", "Tier1", "kelly_fraction", "growth_per_trade",
           "posterior", "size_position"]
