"""OI-based dealer-gamma proxy ("naive GEX").

For every option in the snapshot: dollar gamma per 1% move = gamma × open interest × 100 × spot² × 0.01.
Calls count positive and puts negative — the standard assumption that customers buy puts and sell calls,
leaving dealers long call gamma and short put gamma. The corpus flags the sign baseline as something the
ledger must *measure* (synthesis §8), so this module records a consistent proxy and makes no trading claim.

Outputs: net / call / put GEX, the zero-gamma "flip" level (cumulative net GEX by strike crossing zero),
the call wall (largest call GEX strike), the put wall (largest put GEX strike), a regime label and the
coverage of the inputs. Pure functions; tests in tests/test_daemon_gamma.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class GexInput:
    strike: float
    right: str            # 'C' | 'P'
    gamma: float | None
    open_interest: float | None


def dollar_gamma(gamma: float, oi: float, spot: float) -> float:
    return gamma * oi * 100.0 * spot * spot * 0.01


def dealer_gamma_proxy(rows: Iterable[GexInput], spot: float | None) -> dict[str, Any]:
    rows = list(rows)
    if not spot or spot <= 0 or not rows:
        return {"net_gex": None, "call_gex": None, "put_gex": None, "flip": None, "call_wall": None, "put_wall": None,
                "regime": "unknown", "coverage": 0.0, "n": len(rows)}
    by_strike: dict[float, dict[str, float]] = {}
    covered = 0
    for r in rows:
        if r.gamma is None or r.open_interest is None:
            continue
        covered += 1
        g = dollar_gamma(r.gamma, r.open_interest, spot)
        slot = by_strike.setdefault(r.strike, {"call": 0.0, "put": 0.0})
        if r.right.upper().startswith("C"):
            slot["call"] += g
        else:
            slot["put"] -= g
    coverage = covered / len(rows)
    if not by_strike or coverage == 0:
        return {"net_gex": None, "call_gex": None, "put_gex": None, "flip": None, "call_wall": None, "put_wall": None,
                "regime": "unknown", "coverage": round(coverage, 3), "n": len(rows)}
    call_gex = sum(v["call"] for v in by_strike.values())
    put_gex = sum(v["put"] for v in by_strike.values())
    net = call_gex + put_gex
    strikes = sorted(by_strike)
    # flip: first zero crossing of cumulative net GEX walking strikes upward (linear interpolation)
    flip: float | None = None
    cum = 0.0
    prev_k: float | None = None
    prev_cum = 0.0
    for k in strikes:
        cum += by_strike[k]["call"] + by_strike[k]["put"]
        if prev_k is not None and prev_cum != 0 and (prev_cum < 0 < cum or prev_cum > 0 > cum):
            frac = abs(prev_cum) / (abs(prev_cum) + abs(cum))
            flip = prev_k + (k - prev_k) * frac
            break
        prev_k, prev_cum = k, cum
    call_wall = max(strikes, key=lambda k: by_strike[k]["call"]) if call_gex > 0 else None
    put_wall = min(strikes, key=lambda k: by_strike[k]["put"]) if put_gex < 0 else None   # most negative
    if net > 0:
        regime = "positive"
    elif net < 0:
        regime = "negative"
    else:
        regime = "flat"
    return {
        "net_gex": round(net, 0), "call_gex": round(call_gex, 0), "put_gex": round(put_gex, 0),
        "flip": round(flip, 2) if flip is not None else None,
        "call_wall": call_wall, "put_wall": put_wall, "regime": regime,
        "spot_vs_flip": (None if flip is None else ("above" if spot > flip else "below")),
        "coverage": round(coverage, 3), "n": len(rows), "spot": spot,
    }
