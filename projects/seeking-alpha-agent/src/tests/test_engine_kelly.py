"""Kelly module (M3 acceptance: "Kelly module unit tests reproduce the plan §0.3 table (25%/4R → 6%, 30%/5R → 16%,
35%/6R → 24%)") plus property tests on the sizing rails: never above full Kelly, caps hold, floor only where the plan
allows it, shrinkage keeps n = 0 at the floor and un-shrinks monotonically, k is clamped to [0.5, 1.0]."""
from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from saa_daemon.engine.kelly import (PLAN_TABLE, Posterior, breakeven_p, format_table, growth_per_trade, kelly_fraction, kelly_table,
                                     order_caps, plan_rows, posterior, size_position, trades_to_multiple)
from saa_daemon.engine.tier1 import TIER1


# ------------------------------------------------------------------------------------------------ plan §0.3 table
def test_plan_table_fractions():
    assert [round(kelly_fraction(p, w) * 100) for p, w in PLAN_TABLE] == [6, 16, 24]
    assert kelly_fraction(0.25, 4) == pytest.approx(0.0625)
    assert kelly_fraction(0.30, 5) == pytest.approx(0.16)
    assert kelly_fraction(0.35, 6) == pytest.approx(0.241667, abs=1e-6)


def test_plan_table_growth_and_horizon():
    rows = plan_rows()
    assert [r["f_star_pct"] for r in rows] == [6, 16, 24]
    assert [r["growth_pct"] for r in rows] == [0.7, 5.4, 13.4]           # plan: +0.7% / +5.4% / +13.4% per trade
    assert rows[2]["half_kelly_growth_pct"] == 10.7                        # plan: half-Kelly (12%) → +10.7%/trade
    assert 1900 <= rows[0]["after_96_trades"] <= 2100                      # plan: ≈ $2,000
    assert 150_000 <= rows[1]["after_96_trades"] <= 200_000                # plan: ≈ $180,000
    assert 60 <= rows[2]["trades_to_5m_full"] <= 70                        # plan: $5M by trade ~64
    assert 75 <= rows[2]["trades_to_5m_half"] <= 85                        # plan: half-Kelly → ~80 trades
    txt = format_table(rows)
    assert "f_star_pct" in txt and txt.count("\n") >= 4


def test_breakeven_and_growth_basics():
    assert breakeven_p(3) == pytest.approx(0.25) and breakeven_p(5) == pytest.approx(1 / 6)
    assert kelly_fraction(0.25, 3) == 0.0                     # exactly breakeven → no bet
    assert kelly_fraction(0.10, 3) == 0.0                     # negative edge floored at zero
    assert growth_per_trade(0.3, 5, 0.0) == 0.0
    assert growth_per_trade(0.3, 5, 1.0) == float("-inf")
    assert trades_to_multiple(0.0, 2) is None
    assert trades_to_multiple(math.log(2), 2) == pytest.approx(1.0)


# -------------------------------------------------------------------------------------------------- posterior
def test_posterior_n0_is_barely_positive_edge():
    p0 = posterior([])
    assert p0.n == 0 and p0.p_hat is None and p0.w == TIER1.prior_w and p0.weight == 0.0
    assert p0.p == pytest.approx(breakeven_p(TIER1.prior_w) + TIER1.edge_epsilon)
    assert p0.f_full == pytest.approx(TIER1.edge_epsilon * (1 + 1 / TIER1.prior_w))          # ε·(1 + 1/W)
    assert p0.f_full < 0.01                                                                     # → the floor at $1k


def test_posterior_unshrinks_with_sample_size():
    block = [5.0, -1.0, -1.0, 5.0, -1.0, -1.0, -1.0, 5.0, -1.0, -1.0, 5.0, -1.0, -1.0, -1.0, 5.0, -1.0, 5.0, -1.0, 5.0, 5.0]   # 8/20 wins, W = 5
    wins = block * 5                           # 100 trades, measured p = 0.40, W = 5
    fs = [posterior(wins[:n]).f_full for n in (0, 20, 40, 60, 100)]
    assert fs == sorted(fs) and fs[0] < fs[1] < fs[2] < fs[3] < fs[4]
    p100 = posterior(wins)
    assert p100.p_hat == pytest.approx(0.40) and p100.w_hat == pytest.approx(5.0)
    assert p100.weight == pytest.approx(100 / 130)
    # with the prior mixed in (n0 = 30 at p0 = 0.30, W0 = 5) and 100/130 of the edge usable
    p_mix = (100 * 0.40 + 30 * 0.30) / 130
    assert p100.p_mix == pytest.approx(p_mix)
    assert p100.p == pytest.approx(p100.p_be + TIER1.edge_epsilon + (p_mix - p100.p_be) * 100 / 130)


def test_posterior_negative_edge_gives_zero_kelly():
    losers = [-1.0] * 40 + [0.5] * 10          # p = 0.2 at W = 0.5 → far below breakeven
    post = posterior(losers)
    assert post.p < post.p_be and post.f_full == 0.0


# ------------------------------------------------------------------------------------------------------ sizing
def _known(p: float, w: float) -> Posterior:
    f = kelly_fraction(p, w)
    return Posterior(n=10 ** 6, p_hat=p, w_hat=w, p_mix=p, w=w, p_be=breakeven_p(w), p=p, f_full=f, weight=1.0)


def test_sizing_examples_at_1000():
    post = _known(0.30, 5.0)                                   # f* = 16% → half-Kelly 8% = $80 at $1k
    s = size_position(1000.0, 0.60, post, k=0.5)               # $60 contracts → 1 contract ($60 ≤ $80)
    assert s.contracts == 1 and s.mode == "kelly" and s.risk_dollars == 60.0 and s.f_used == pytest.approx(0.08)
    s = size_position(1000.0, 1.20, post, k=0.5)               # $120 > $80 → Kelly says 0 → floor (≤ $150, account < $2k)
    assert s.contracts == 1 and s.mode == "floor" and "one-contract floor" in " ".join(s.reasons)
    s = size_position(1000.0, 1.80, post, k=0.5)               # $180 > $150 → floor not applicable → no trade
    assert s.contracts == 0 and s.mode == "none"
    s = size_position(1000.0, 0.60, post, k=1.0)               # full Kelly 16% = $160 → 2 contracts
    assert s.contracts == 2 and s.mode == "kelly" and s.f_used == pytest.approx(0.16)
    s = size_position(1000.0, 0.60, post, k=0.5, cooling_off=True)    # half size → $40 → 0 → floor
    assert s.contracts == 1 and s.mode == "floor" and "cooling-off" in " ".join(s.reasons)


def test_sizing_halt_modes_and_k_clamp():
    post = _known(0.35, 6.0)                                   # f* = 24%
    assert size_position(1000.0, 0.50, post, k=1.0).contracts == 2       # cap: 2 contracts per $1k (Kelly would say 4)
    s = size_position(1000.0, 0.50, post, k=1.0, halt_mode="floor")
    assert s.contracts == 1 and s.mode == "halt-floor"
    s = size_position(1000.0, 0.50, post, k=1.0, halt_mode="stop")
    assert s.contracts == 0 and s.mode == "halt-stop"
    s = size_position(1000.0, 0.50, post, k=2.0)                          # k clamped to 1.0
    assert s.k == 1.0 and any("clamped" in r for r in s.reasons)
    s = size_position(1000.0, 0.50, post, k=0.1)                          # k clamped to 0.5
    assert s.k == 0.5
    assert order_caps(1000.0) == (2, 500.0) and order_caps(5000.0) == (10, 2500.0)


def test_kelly_table_shape():
    rows = kelly_table(1000.0)
    assert len(rows) == 20 and all({"p", "w", "f_star", "contracts@0.50", "contracts@1.50"} <= set(r) for r in rows)
    r = next(r for r in rows if r["p"] == 0.35 and r["w"] == 6.0)
    assert r["f_star"] == 0.2417 and r["contracts@0.50"] == 2            # 24% × $1k = $240 → 4 contracts, capped at 2


# ------------------------------------------------------------------------------------------- property tests
acct = st.floats(min_value=200.0, max_value=250_000.0, allow_nan=False, allow_infinity=False)
prem = st.floats(min_value=0.01, max_value=60.0, allow_nan=False, allow_infinity=False)
pst = st.floats(min_value=0.01, max_value=0.99)
wst = st.floats(min_value=0.5, max_value=30.0)
kst = st.floats(min_value=0.0, max_value=3.0)


@settings(max_examples=400, deadline=None)
@given(account=acct, premium=prem, p=pst, w=wst, k=kst, halt=st.sampled_from(["none", "floor", "stop"]), cooling=st.booleans())
def test_rail_never_above_full_kelly_except_the_documented_floor(account, premium, p, w, k, halt, cooling):
    post = _known(p, w)
    s = size_position(account, premium, post, k=k, halt_mode=halt, cooling_off=cooling)
    per = premium * 100.0
    max_c, max_prem = order_caps(account)
    assert s.contracts >= 0 and s.risk_dollars == pytest.approx(s.contracts * per)
    assert s.contracts <= max_c and s.risk_dollars <= max_prem + 1e-9                     # order sanity caps always hold
    assert TIER1.kelly_k_min <= s.k <= TIER1.kelly_k_max                                  # k clamped
    if halt == "stop":
        assert s.contracts == 0
    if s.contracts > 1 or (s.contracts == 1 and s.mode in ("kelly", "capped")):
        assert s.risk_dollars <= post.f_full * account + 1e-9                           # never above full Kelly ...
    elif s.contracts == 1:
        # ... the one exception: the single-contract floor, only while account < $2k and the contract costs ≤ $150
        assert s.mode in ("floor", "halt-floor") and account < TIER1.floor_account_below and per <= TIER1.floor_premium_max
    if halt == "floor" and s.contracts:
        assert s.contracts == 1


@settings(max_examples=300, deadline=None)
@given(rs=st.lists(st.floats(min_value=-1.0, max_value=30.0, allow_nan=False), max_size=200))
def test_posterior_is_bounded_and_prior_weighted(rs):
    post = posterior(rs)
    assert 0.0 <= post.p <= 1.0 and post.w > 0 and 0.0 <= post.weight < 1.0
    assert post.f_full <= kelly_fraction(1.0, post.w)
    if not rs:
        assert post.f_full == pytest.approx(TIER1.edge_epsilon * (1 + 1 / TIER1.prior_w))
    # the prior always has weight: with n trades the data can move p_mix at most n/(n+n0) of the way from the prior
    assert abs(post.p_mix - TIER1.prior_p) <= max(TIER1.prior_p, 1 - TIER1.prior_p) * post.weight + 1e-12


@settings(max_examples=200, deadline=None)
@given(n=st.integers(min_value=0, max_value=300))
def test_shrinkage_monotone_for_a_fixed_positive_edge(n):
    rs = ([6.0, -1.0, -1.0] * 100)
    a, b = posterior(rs[:n]), posterior(rs[: n + 1])
    assert b.weight >= a.weight
