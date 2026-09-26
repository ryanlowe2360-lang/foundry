"""Independent Python reference for the M1 shadow-trade model (saa.bs_price / saa.t_years /
saa.score_shadow_trade in src/supabase/migrations/0001_saa_schema.sql).

The in-database test `select saa.test_shadow_model()` is the primary check. This file re-implements
the same model in Python and asserts the numbers the database produced on 2026-09-26, so a change to
either side that drifts the model is caught. Run: `python3 -m pytest src/tests -q` (or `python3 src/tests/test_shadow_model.py`).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# --- values recorded from `select saa.test_shadow_model()` on 2026-09-26 (Quant edge, schema saa) ---
DB_E2E = {"entry_premium": 1.3636, "exit_premium": 2.3098, "r_result": 0.6939, "mfe_r": 1.1625, "mae_r": 0.0, "exit_reason": "trail"}
DB_TIME_STOP = {"r_result": -0.0924, "exit_reason": "time_stop"}


def norm_cdf(x: float) -> float:
    # Abramowitz & Stegun 7.1.26, same as saa.norm_cdf
    ax = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * ax)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * math.exp(-ax * ax)
    return 0.5 * (1.0 + y) if x >= 0 else 0.5 * (1.0 - y)


def bs_price(s: float, k: float, sigma_d: float, t_years: float, opt_type: str) -> float:
    if s <= 0 or k <= 0:
        return 0.0
    if t_years <= 0 or sigma_d <= 0:
        return max(s - k, 0.0) if opt_type == "call" else max(k - s, 0.0)
    sig = sigma_d * math.sqrt(252.0)
    sqt = math.sqrt(t_years)
    d1 = (math.log(s / k) + 0.5 * sig * sig * t_years) / (sig * sqt)
    d2 = d1 - sig * sqt
    if opt_type == "call":
        px = s * norm_cdf(d1) - k * norm_cdf(d2)
    else:
        px = k * norm_cdf(-d2) - s * norm_cdf(-d1)
    return round(max(px, 0.0), 4)


def t_years(ts: datetime, expiry: datetime) -> float:
    """Trading-clock year fraction: whole business days + today's remaining session (09:30–16:00 ET)."""
    if ts >= expiry:
        return 0.0
    a, b = ts.astimezone(ET), expiry.astimezone(ET)
    open_t, close_t = timedelta(hours=9, minutes=30), timedelta(hours=16)

    def tod(d: datetime) -> timedelta:
        return timedelta(hours=d.hour, minutes=d.minute, seconds=d.second, microseconds=d.microsecond)

    if a.date() == b.date():
        secs = max(0.0, (min(tod(b), close_t) - max(tod(a), open_t)).total_seconds())
        return (secs / 23400.0) / 252.0
    secs = max(0.0, (close_t - max(tod(a), open_t)).total_seconds())
    days = 0
    d = a.date() + timedelta(days=1)
    while d < b.date():
        if d.isoweekday() <= 5:
            days += 1
        d += timedelta(days=1)
    secs += max(0.0, (min(tod(b), close_t) - open_t).total_seconds())
    return (days + secs / 23400.0) / 252.0


def et(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def score(entry_mid: float, entry_ask: float, spread_frac: float, strike: float, sigma_d: float, opt_type: str,
          expiry: datetime, ticks: list[tuple[datetime, float]], window_end: datetime, now: datetime,
          account: float = 1000.0, bank_frac: float = 0.075, trail_frac: float = 0.30, tight_frac: float = 0.20,
          tight_at: float = 3.0, atr_n: int = 10, min_spread_abs: float = 0.02) -> dict:
    """Port of saa.score_shadow_trade for one trade."""
    spread_abs = max(entry_mid * spread_frac, min_spread_abs)
    activation_r = (bank_frac * account) / (entry_ask * 100.0)
    hwm, mfe, mae, trail_on = entry_ask, 0.0, 0.0, False
    diffs: list[float] = []
    prev_mid = None
    last = None
    n = 0
    for ts, price in ticks:
        if not (ts <= min(window_end, now)):
            continue
        n += 1
        mid = bs_price(price, strike, sigma_d, t_years(ts, expiry), opt_type)
        bid = max(mid - spread_abs / 2, 0.0)
        if prev_mid is not None:
            diffs.append(abs(mid - prev_mid))
            if len(diffs) > atr_n:
                diffs = diffs[1:]
        prev_mid = mid
        gain_r = (bid - entry_ask) / entry_ask
        mfe, mae = max(mfe, gain_r), min(mae, gain_r)
        hwm = max(hwm, bid)
        if not trail_on and gain_r >= activation_r:
            trail_on = True
        if trail_on:
            atr = sum(diffs) / len(diffs) if diffs else 0.0
            stop = hwm - max((tight_frac if gain_r >= tight_at else trail_frac) * (hwm - entry_ask), atr)
            if bid <= stop:
                return {"exit_reason": "trail", "exit_premium": round(bid, 4), "r_result": round((bid - entry_ask) / entry_ask, 4),
                        "mfe_r": round(mfe, 4), "mae_r": round(mae, 4), "trail_activated": True, "ticks_used": n}
        last = (ts, price, bid)
    if now >= window_end and last is not None:
        bid = last[2]
        return {"exit_reason": "expiry" if window_end >= expiry else "time_stop", "exit_premium": round(bid, 4),
                "r_result": round((bid - entry_ask) / entry_ask, 4), "mfe_r": round(mfe, 4), "mae_r": round(mae, 4),
                "trail_activated": trail_on, "ticks_used": n}
    return {"exit_reason": None}


def open_trade(underlying: float, direction: str, sigma_d: float, strike_step: float, spread_frac: float,
               at: datetime, expiry: datetime, otm_sigma: float = 0.5, min_spread_abs: float = 0.02) -> dict:
    opt_type = "call" if direction == "long" else "put"
    sign = 1 if direction == "long" else -1
    strike = round(underlying * (1 + sign * otm_sigma * sigma_d) / strike_step) * strike_step
    mid = bs_price(underlying, strike, sigma_d, t_years(at, expiry), opt_type)
    ask = round(mid + max(mid * spread_frac, min_spread_abs) / 2, 4)
    return {"strike": strike, "option_type": opt_type, "entry_mid": mid, "entry_premium": ask}


# ----------------------------------------------------------------------------------------------- tests

def test_norm_cdf_and_bs_basics():
    assert abs(norm_cdf(0) - 0.5) < 1e-6
    assert abs(norm_cdf(1.959964) - 0.975) < 1e-5
    assert abs(bs_price(100, 100, 0.01, 1 / 252, "call") - 0.3989) < 0.002          # ATM ≈ 0.3989·S·σ√T
    c, p = bs_price(100, 95, 0.02, 3 / 252, "call"), bs_price(100, 95, 0.02, 3 / 252, "put")
    assert abs((c - p) - 5) < 1e-3                                                # put-call parity, r = 0
    assert bs_price(102, 100, 0.02, 0, "call") == 2                               # intrinsic at expiry


def test_t_years_conventions():
    assert abs(t_years(et(2026, 9, 28, 9, 30), et(2026, 9, 28, 16, 0)) - 1 / 252) < 1e-12
    # Monday 09:45 → Friday 16:00: 6.25/6.5 of Monday + Tue/Wed/Thu + Friday session
    t = t_years(et(2026, 9, 28, 9, 45), et(2026, 10, 2, 16, 0))
    assert abs(t - (6.25 / 6.5 + 3 + 1) / 252) < 1e-12


def test_e2e_matches_database():
    """Same synthetic path as saa.test_shadow_model(): ZZTEST single name, σ_d 2%, long at 100 on Mon 09:45."""
    t0, expiry, window_end = et(2026, 9, 28, 9, 45), et(2026, 10, 2, 16, 0), et(2026, 9, 28, 10, 0)
    o = open_trade(100.0, "long", 0.02, 1.0, 0.05, t0, expiry)
    assert o["strike"] == 101 and o["option_type"] == "call"
    assert o["entry_premium"] == DB_E2E["entry_premium"], o
    ticks = [(t0 + timedelta(minutes=i), 100 + i * 0.375 if i <= 8 else 103 - (i - 8) * 0.5) for i in range(1, 15)]
    r = score(o["entry_mid"], o["entry_premium"], 0.05, o["strike"], 0.02, "call", expiry, ticks, window_end, et(2026, 9, 28, 10, 5))
    assert r["exit_reason"] == "trail" and r["trail_activated"]
    assert r["exit_premium"] == DB_E2E["exit_premium"], r
    assert r["r_result"] == DB_E2E["r_result"], r
    assert r["mfe_r"] == DB_E2E["mfe_r"] and r["mae_r"] == DB_E2E["mae_r"], r


def test_time_stop_matches_database():
    t0, expiry, window_end = et(2026, 9, 28, 10, 5), et(2026, 10, 2, 16, 0), et(2026, 9, 28, 11, 30)
    o = open_trade(100.0, "short", 0.02, 1.0, 0.05, t0, expiry)
    assert o["strike"] == 99 and o["option_type"] == "put"
    ticks = [(t0 + timedelta(minutes=i), 100.0 + (i % 3) * 0.05) for i in range(1, 86)]
    r = score(o["entry_mid"], o["entry_premium"], 0.05, o["strike"], 0.02, "put", expiry, ticks, window_end, et(2026, 9, 28, 11, 35))
    assert r["exit_reason"] == "time_stop" and r["r_result"] == DB_TIME_STOP["r_result"], r


def test_kelly_table_from_plan():
    """Build plan §0.3: f* = p − (1−p)/W → 6% / 16% / 24%."""
    for p, w, f in [(0.25, 4, 0.0625), (0.30, 5, 0.16), (0.35, 6, 0.2417)]:
        assert abs((p - (1 - p) / w) - f) < 5e-3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
