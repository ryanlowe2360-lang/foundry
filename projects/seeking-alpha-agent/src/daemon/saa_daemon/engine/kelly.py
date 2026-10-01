"""Kelly module — `size = k · f*(posterior p, posterior W)` with shrinkage toward breakeven (plan §0.2–0.3, §3 Tier 1).

Definitions (accountant level):
* R = the premium paid for one contract (× 100 shares) — the most a long option can lose.
* p = hit rate, W = average win in R. Breakeven hit rate for a payoff W is p_be = 1 / (1 + W).
* Kelly fraction f* = p − (1 − p) / W = the fraction of the account that maximizes long-run compound growth if p and W
  are exactly known. Growth per trade (geometric) = p·ln(1 + f·W) + (1 − p)·ln(1 − f).
* Plan §0.3 table: (25%, 4R) → 6%, (30%, 5R) → 16%, (35%, 6R) → 24% (reproduced by tests/test_engine_kelly.py).

Posterior (what the ledger has measured so far, shrunk):
1. Mix the measured hit rate / win size with the corpus prior as if the prior were worth n0 = 30 trades:
   p_mix = (n·p̂ + n0·p0) / (n + n0), W = (n·Ŵ + n0·W0) / (n + n0).
2. Shrink the *edge* toward breakeven: p = p_be(W) + ε + (p_mix − p_be(W)) · n / (n + n0).
   With n = 0 the usable edge is ε only → f* = ε · (1 + 1/W) ≈ 0.6% → the one-contract floor. By n = 30 half the measured
   edge counts, by n = 90 three quarters. A measured negative edge pulls p below breakeven → f* = 0 → no size (the
   edge-loss halt in rails.py makes the same call from the rolling expectancy).

Sizing: contracts = ⌊k · f* · account / (premium · 100)⌋, capped at full Kelly (never above f* · account), capped by the
order sanity caps, floored at one contract only while the account is < $2k and the contract costs ≤ $150 (the plan's single
documented exception: 0.25R adds are inexpressible at $1k). Halt modes: "floor" → exactly the floor; "stop" → 0.
Pure functions; no I/O.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .tier1 import TIER1, Tier1


def breakeven_p(w: float) -> float:
    return 1.0 / (1.0 + w)


def kelly_fraction(p: float, w: float) -> float:
    """f* = p − (1 − p)/W, floored at 0 (a negative edge means no bet)."""
    if w <= 0:
        return 0.0
    return max(0.0, p - (1.0 - p) / w)


def growth_per_trade(p: float, w: float, f: float) -> float:
    """Geometric growth per trade for a bet of fraction f. −inf when f ≥ 1 (ruin on a loss)."""
    if f <= 0:
        return 0.0
    if f >= 1:
        return float("-inf")
    return p * math.log1p(f * w) + (1.0 - p) * math.log1p(-f)


def trades_to_multiple(growth: float, multiple: float) -> float | None:
    """How many trades at `growth` per trade it takes to multiply the account by `multiple`."""
    if growth <= 0:
        return None
    return math.log(multiple) / growth


@dataclass(frozen=True)
class Posterior:
    n: int
    p_hat: float | None     # measured hit rate (None when n = 0)
    w_hat: float | None     # measured average win in R (None when no winner yet)
    p_mix: float            # prior-mixed hit rate
    w: float                # prior-mixed payoff
    p_be: float             # breakeven hit rate for w
    p: float                # usable (edge-shrunk) hit rate
    f_full: float           # full Kelly of the posterior
    weight: float           # n / (n + n0)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


def posterior(results_r: Iterable[float], tier1: Tier1 = TIER1) -> Posterior:
    """Posterior (p, W) from a list of closed-trade results in R (gate-fired trades only; fast-lane hypotheses excluded)."""
    rs = [float(r) for r in results_r]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    p_hat = (len(wins) / n) if n else None
    w_hat = (sum(wins) / len(wins)) if wins else None
    n0 = float(tier1.shrink_n0)
    weight = n / (n + n0) if n else 0.0
    p_mix = ((n * p_hat) + n0 * tier1.prior_p) / (n + n0) if n else tier1.prior_p
    w = ((n * (w_hat if w_hat is not None else tier1.prior_w)) + n0 * tier1.prior_w) / (n + n0) if n else tier1.prior_w
    p_be = breakeven_p(w)
    p = p_be + tier1.edge_epsilon + (p_mix - p_be) * weight
    p = min(max(p, 0.0), 1.0)
    return Posterior(n=n, p_hat=p_hat, w_hat=w_hat, p_mix=p_mix, w=w, p_be=p_be, p=p, f_full=kelly_fraction(p, w), weight=weight)


@dataclass(frozen=True)
class Sizing:
    contracts: int
    premium_per_contract: float   # dollars (premium × 100)
    risk_dollars: float           # contracts × premium_per_contract
    risk_frac: float              # risk_dollars / account
    f_full: float                 # full Kelly of the posterior
    f_used: float                 # k · f_full (× 0.5 in a cooling-off session)
    k: float
    mode: str                     # kelly | floor | capped | halt-floor | halt-stop | none
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


def order_caps(account: float, tier1: Tier1 = TIER1) -> tuple[int, float]:
    """(max contracts per order, max premium dollars per order) from the account size."""
    max_contracts = tier1.max_contracts_per_1000 * max(1, int(account // 1000))
    return max_contracts, tier1.max_premium_frac_of_account * account


def size_position(account: float, premium: float, post: Posterior, *, k: float, halt_mode: str = "none",
                  cooling_off: bool = False, tier1: Tier1 = TIER1) -> Sizing:
    """Whole-contract size for one entry. `premium` is the option ask per share; `halt_mode` ∈ none | floor | stop."""
    reasons: list[str] = []
    k_used = min(max(float(k), tier1.kelly_k_min), tier1.kelly_k_max)
    if k_used != k:
        reasons.append(f"k clamped {k}→{k_used}")
    per = max(premium, 0.0) * 100.0
    if per <= 0 or account <= 0:
        return Sizing(0, per, 0.0, 0.0, post.f_full, 0.0, k_used, "none", tuple(reasons + ["no premium/account"]))
    f_used = k_used * post.f_full
    if cooling_off:
        f_used *= tier1.cooling_off_size_mult
        reasons.append("cooling-off: half size")
    max_contracts, max_premium = order_caps(account, tier1)
    floor_ok = account < tier1.floor_account_below and per <= tier1.floor_premium_max and per <= max_premium

    if halt_mode == "stop":
        return Sizing(0, per, 0.0, 0.0, post.f_full, f_used, k_used, "halt-stop", tuple(reasons + ["edge-loss halt: rolling-60 expectancy < 0"]))

    kelly_contracts = int(math.floor(f_used * account / per))
    full_cap = int(math.floor(post.f_full * account / per))          # never above full Kelly
    contracts = min(kelly_contracts, full_cap)
    mode = "kelly"
    if halt_mode == "floor":
        contracts = 0
        mode = "halt-floor"
        reasons.append("edge-loss halt: rolling-30 expectancy < 0 → floor")
    if contracts > max_contracts:
        contracts, mode = max_contracts, "capped"
        reasons.append(f"cap: ≤{max_contracts} contracts")
    while contracts > 0 and contracts * per > max_premium:
        contracts -= 1
        mode = "capped"
        reasons.append(f"cap: premium ≤ ${max_premium:.0f}")
    if contracts == 0:
        if floor_ok:
            contracts = 1
            mode = "floor" if mode != "halt-floor" else mode
            reasons.append("one-contract floor (account < $2k, premium ≤ $150)")
        else:
            mode = "none" if mode == "kelly" else mode
            reasons.append("Kelly size < 1 contract and floor not applicable")
    risk = contracts * per
    return Sizing(contracts, per, risk, risk / account if account else 0.0, post.f_full, f_used, k_used, mode, tuple(reasons))


# ---------------------------------------------------------------------------------------------------------- tables
PLAN_TABLE = ((0.25, 4.0), (0.30, 5.0), (0.35, 6.0))   # plan §0.3


def plan_rows() -> list[dict[str, Any]]:
    out = []
    for p, w in PLAN_TABLE:
        f = kelly_fraction(p, w)
        g = growth_per_trade(p, w, f)
        gh = growth_per_trade(p, w, f / 2)
        out.append({"p": p, "w": w, "f_star": round(f, 4), "f_star_pct": round(f * 100), "growth_pct": round(g * 100, 1),
                    "half_kelly_growth_pct": round(gh * 100, 1), "after_96_trades": round(1000 * math.exp(96 * g)),
                    "trades_to_5m_full": (round(trades_to_multiple(g, 5000)) if g > 0 else None),
                    "trades_to_5m_half": (round(trades_to_multiple(gh, 5000)) if gh > 0 else None)})
    return out


def kelly_table(account: float = 1000.0, premiums: Iterable[float] = (0.50, 1.00, 1.50), k: float = 0.5,
                grid: Iterable[tuple[float, float]] | None = None, tier1: Tier1 = TIER1) -> list[dict[str, Any]]:
    """Sizes for a grid of (p, W) at several premiums — the printable Kelly table (`python -m saa_daemon kelly-table`)."""
    grid = list(grid) if grid is not None else [(p, w) for p in (0.20, 0.25, 0.30, 0.35, 0.40) for w in (3.0, 4.0, 5.0, 6.0)]
    rows = []
    for p, w in grid:
        f = kelly_fraction(p, w)
        row: dict[str, Any] = {"p": p, "w": w, "p_be": round(breakeven_p(w), 4), "f_star": round(f, 4),
                               "growth_pct": round(growth_per_trade(p, w, f) * 100, 2),
                               "half_growth_pct": round(growth_per_trade(p, w, f * k) * 100, 2)}
        # a "known edge" posterior for the table: n large enough that shrinkage is ~gone (illustrative only)
        post = Posterior(n=10 ** 6, p_hat=p, w_hat=w, p_mix=p, w=w, p_be=breakeven_p(w), p=p, f_full=f, weight=1.0)
        for prem in premiums:
            s = size_position(account, prem, post, k=k, tier1=tier1)
            row[f"contracts@{prem:.2f}"] = s.contracts
        rows.append(row)
    return rows


def format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    cols = list(rows[0])
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    head = "  ".join(c.rjust(widths[c]) for c in cols)
    lines = [head, "  ".join("-" * widths[c] for c in cols)]
    for r in rows:
        lines.append("  ".join(str(r.get(c, "")).rjust(widths[c]) for c in cols))
    return "\n".join(lines)
