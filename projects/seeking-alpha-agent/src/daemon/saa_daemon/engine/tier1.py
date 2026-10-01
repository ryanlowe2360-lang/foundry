"""Tier 1 — the immutable survival rails (build plan v0.4 §3, Tier 1).

These numbers live in code on purpose: the plan says Tier 1 is "not overridable by Claude or by Ryan mid-session".
A `saa.rules` row that carries a different value for one of these keys is ignored (and logged) — see `rules.Rules`.
Changing a Tier 1 value is a spec change (Foundry Improve addendum), never a review action.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class Tier1:
    # --- sizing formula (inputs learned, formula fixed) -------------------------------------------------------------
    kelly_k_min: float = 0.5           # k ∈ [0.5, 1.0]; 0.5 at M0, ratchet only at a weekly review (M6)
    kelly_k_max: float = 1.0
    shrink_n0: int = 30                # posterior shrinkage weight toward breakeven: n/(n+n0); n0 = 30 trades
    prior_p: float = 0.30              # corpus mid-case prior (30% hit, 5R) — un-shrinks as the ledger grows
    prior_w: float = 5.0
    edge_epsilon: float = 0.005        # n = 0 → "edge barely positive": f* = epsilon·(1 + 1/W), i.e. the floor
    floor_account_below: float = 2000.0  # one-contract floor applies while the account is < $2k ...
    floor_premium_max: float = 150.0     # ... and the contract costs ≤ $150 (plan: "≤ $100–150")
    # never above full Kelly of the posterior (the floor is the single documented exception, see kelly.size_position)
    # --- order sanity caps (from account size) ----------------------------------------------------------------------
    max_premium_frac_of_account: float = 0.50   # a single order never risks more than half the account ...
    max_contracts_per_1000: int = 2             # ... nor more than 2 contracts per $1,000 of account
    # --- edge-loss halt (replaces the drawdown halt) ----------------------------------------------------------------
    halt_floor_window: int = 30        # rolling 30-trade expectancy < 0 → size at the floor
    halt_stop_window: int = 60         # rolling 60-trade expectancy < 0 → no new entries pending a written review
    # --- day rails --------------------------------------------------------------------------------------------------
    daily_stop_r: float = -3.0         # realized + banked R today ≤ −3 → no fresh entries
    consecutive_loss_lockout: int = 3  # 3 straight losing closes in a day → no fresh entries that day
    cooling_off_win_r: float = 5.0     # any ≥ +5R win ...
    cooling_off_day_r: float = 8.0     # ... or a ≥ +8R day → next session at half size
    cooling_off_size_mult: float = 0.5
    # --- clock ------------------------------------------------------------------------------------------------------
    no_entry_start: time = time(11, 30)   # no fresh entries 11:30–13:30 ET
    no_entry_end: time = time(13, 30)
    release_blackout_minutes: int = 15    # never own an option through a scheduled release: flat from T−15 to T
    last_entry_before_close_minutes: int = 5   # every 0DTE position is out by close − 5 min (time stop at window edge)
    # --- spread filter ----------------------------------------------------------------------------------------------
    spread_flag_frac: float = 0.05     # 5–10% of premium → flagged
    spread_skip_frac: float = 0.10     # > 10% of premium → skip


TIER1 = Tier1()

# Keys a saa.rules row may carry that are Tier 1 here. The engine refuses to read them from data.
TIER1_KEYS_IN_RULES = ("daily_stop_r", "consecutive_loss_lockout", "cooling_off", "no_entry_et", "spread_filter", "kelly",
                       "order_caps", "edge_loss_halt")
