"""Shadow positions and their exits: partial bank at +7.5% of account, then a trail of max(30% of the gain, one
1-minute option ATR) tightened to 20% once ≥ +3R; mechanism exits (loss of VWAP for single names, failed new extreme,
volume taper) that override the trail; the window-edge time stop; expiry. Everything is marked at the bid — mid minus
half the spread — from DXLink, which is what a seller actually gets (plan §4–5; M1 model D5 used the same convention).

R for a position = contracts × entry ask × 100 (the premium paid). `r_result` = (proceeds − cost) / cost, so a full loss
is −1R whatever the contract count, and partial banks count toward the result.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Sequence

from ..bars import Bar
from ..clock import et
from .rules import Rules
from .triggers import failed_new_extreme, session_vwap, volume_taper


@dataclass(frozen=True)
class Exit:
    reason: str
    note: str


def _r6(v: float | None) -> float | None:
    return None if v is None else round(float(v), 6)


@dataclass
class Position:
    key: str
    trade_date: date
    symbol: str
    is_index: bool
    source: str                # gate | fast_lane
    lane: str                  # open | event | presser | last_hour | orb | vwap | continuation | rvol
    window: str
    direction: str             # long | short
    option_symbol: str
    option_type: str           # call | put
    strike: float
    expiration: date
    dte: int
    opened_at: datetime
    entry_bar_time: datetime
    entry_bid: float
    entry_ask: float
    entry_iv: float | None
    entry_spot: float
    sigma_d: float
    contracts: int
    time_stop: datetime
    time_stop_reason: str      # window | release | close
    account: float
    sizing: dict[str, Any]
    probability: float | None = None
    checklist_id: int | None = None
    gates: dict[str, Any] | None = None
    counts_for_rails: bool = True
    # running state
    open_contracts: int = 0
    hwm_bid: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    trail_on: bool = False
    trail_tight: bool = False      # latched once the gain has reached tight_at_r (a dip below does not loosen the stop)
    trail_stop: float | None = None
    banked_contracts: int = 0
    banked_proceeds: float = 0.0
    banked_at: datetime | None = None
    last_bid: float | None = None
    last_ask: float | None = None
    last_mark_at: datetime | None = None
    last_spot: float | None = None
    marks: int = 0
    diffs: deque = field(default_factory=lambda: deque(maxlen=10))
    prev_mid: float | None = None
    status: str = "open"
    exit_reason: str | None = None
    exit_note: str | None = None
    exit_at: datetime | None = None
    exit_bid: float | None = None
    exit_spot: float | None = None
    r_result: float | None = None
    proceeds: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.open_contracts = self.contracts
        self.hwm_bid = self.entry_bid
        self.last_bid, self.last_ask = self.entry_bid, self.entry_ask
        self.last_spot = self.entry_spot

    # ------------------------------------------------------------------------------------------------- accounting
    @property
    def cost(self) -> float:
        return self.contracts * self.entry_ask * 100.0

    def gain_r(self, bid: float) -> float:
        """Per-share gain in R: (bid − entry ask) / entry ask."""
        return (bid - self.entry_ask) / self.entry_ask if self.entry_ask > 0 else 0.0

    def open_pnl(self, bid: float) -> float:
        return self.banked_proceeds + self.open_contracts * bid * 100.0 - self.cost

    # -------------------------------------------------------------------------------------------------- the mark
    def on_mark(self, now: datetime, bid: float | None, ask: float | None, spot: float | None, rules: Rules) -> Exit | None:
        """Apply a fresh option mark. Returns an Exit when the trail is hit."""
        if self.status != "open" or bid is None or ask is None:
            return None
        bid = max(0.0, float(bid))
        mid = (bid + float(ask)) / 2.0
        self.marks += 1
        self.last_bid, self.last_ask, self.last_mark_at = bid, float(ask), now
        if spot:
            self.last_spot = spot
        if self.prev_mid is not None:
            self.diffs.append(abs(mid - self.prev_mid))
        self.prev_mid = mid
        g = self.gain_r(bid)
        self.mfe_r, self.mae_r = max(self.mfe_r, g), min(self.mae_r, g)
        self.hwm_bid = max(self.hwm_bid, bid)
        t = rules.trail
        if not self.trail_on and self.open_pnl(bid) >= rules.bank_frac * self.account:
            self.trail_on = True
            # bank: sell enough contracts to return the premium to cash where the contract count allows
            need = int(-(-self.cost // (bid * 100.0))) if bid > 0 else self.open_contracts   # ceil
            if 0 < need < self.open_contracts:
                self.banked_contracts = need
                self.banked_proceeds = need * bid * 100.0
                self.banked_at = now
                self.open_contracts -= need
                self.events.append({"at": et(now).strftime("%H:%M"), "event": "bank", "contracts": need, "bid": _r6(bid)})
            else:
                self.events.append({"at": et(now).strftime("%H:%M"), "event": "trail_on", "bid": _r6(bid), "note": "one contract: whole position trails"})
        if self.trail_on:
            atr = (sum(self.diffs) / len(self.diffs)) if self.diffs else 0.0
            if g >= float(t["tight_at_r"]):
                self.trail_tight = True
            frac = float(t["tight_frac"]) if self.trail_tight else float(t["frac"])
            self.trail_stop = self.hwm_bid - max(frac * (self.hwm_bid - self.entry_ask), atr)
            if bid <= self.trail_stop:
                return Exit("trail", f"bid {bid:.2f} ≤ stop {self.trail_stop:.2f} (hwm {self.hwm_bid:.2f}, {frac:.0%}/ATR {atr:.3f})")
        return None

    # ---------------------------------------------------------------------------------------------- bar-based exits
    def on_bar_close(self, bars: Sequence[Bar], rules: Rules) -> Exit | None:
        """Mechanism exits on the newest completed bar of the underlying (override the trail)."""
        if self.status != "open" or not bars:
            return None
        last = bars[-1]
        if last.start <= self.entry_bar_time:
            return None
        ex = rules.exits
        vl = ex["vwap_loss"]
        if vl.get("enabled") and (not vl.get("single_names_only", True) or not self.is_index):
            n = int(vl.get("bars", 1))
            recent = [b for b in bars if b.start > self.entry_bar_time][-n:]
            if len(recent) >= n:
                vw = session_vwap(bars)
                if vw is not None:
                    lost = all((b.close < vw) if self.direction == "long" else (b.close > vw) for b in recent)
                    if lost:
                        return Exit("vwap_loss", f"{n} close(s) {'below' if self.direction == 'long' else 'above'} VWAP {vw:.2f}")
        fe = ex["failed_extreme"]
        if fe.get("enabled") and failed_new_extreme(bars, self.direction):
            return Exit("failed_extreme", "new session extreme sold back inside (push sold into)")
        vt = ex["volume_taper"]
        if vt.get("enabled") and volume_taper(bars, self.entry_bar_time, n=int(vt["bars"]), frac=float(vt["frac"])):
            return Exit("volume_taper", f"last {vt['bars']} bars' volume < {float(vt['frac']):.0%} of the entry burst")
        return None

    def time_exit(self, now: datetime) -> Exit | None:
        if self.status == "open" and now >= self.time_stop:
            reason = {"window": "time_stop", "release": "release_blackout", "close": "expiry" if self.expiration == self.trade_date else "time_stop"}[self.time_stop_reason]
            return Exit(reason, f"time stop {et(self.time_stop):%H:%M} ({self.time_stop_reason})")
        return None

    # -------------------------------------------------------------------------------------------------------- close
    def close(self, now: datetime, exit_: Exit, bid: float | None = None) -> None:
        if self.status != "open":
            return
        b = self.last_bid if bid is None else max(0.0, float(bid))
        if b is None:
            b = 0.0
        self.exit_bid = b
        self.exit_spot = self.last_spot
        self.exit_at = now
        self.exit_reason = exit_.reason
        self.exit_note = exit_.note
        self.proceeds = self.banked_proceeds + self.open_contracts * b * 100.0
        self.r_result = (self.proceeds - self.cost) / self.cost if self.cost > 0 else 0.0
        self.status = "closed"

    def void(self, now: datetime, note: str) -> None:
        self.status = "void"
        self.exit_at = now
        self.exit_reason = "void_no_marks"
        self.exit_note = note

    # ---------------------------------------------------------------------------------------------------------- rows
    def row(self) -> dict[str, Any]:
        """Canonical ledger row (shape of saa.shadow_trades + engine extras). Floats rounded for byte-stable output."""
        return {
            "engine_key": self.key, "trade_date": self.trade_date.isoformat(), "symbol": self.symbol, "lane": self.lane, "source": "engine",
            "engine_source": self.source, "window": self.window, "direction": self.direction, "option_symbol": self.option_symbol,
            "option_type": self.option_type, "strike": _r6(self.strike), "expiration": self.expiration.isoformat(), "dte": self.dte,
            "opened_at": self.opened_at.isoformat(), "window_end": self.time_stop.isoformat(), "time_stop_reason": self.time_stop_reason,
            "entry_underlying": _r6(self.entry_spot), "entry_bid": _r6(self.entry_bid), "entry_mid": _r6((self.entry_bid + self.entry_ask) / 2.0),
            "entry_premium": _r6(self.entry_ask), "entry_iv": _r6(self.entry_iv), "sigma_d": _r6(self.sigma_d),
            "spread_frac": _r6((self.entry_ask - self.entry_bid) / self.entry_ask if self.entry_ask else None),
            "contracts": self.contracts, "cost": _r6(self.cost), "size_r": _r6(self.cost / self.account if self.account else None), "sizing": self.sizing,
            "probability": self.probability, "checklist_id": self.checklist_id, "gates": self.gates, "counts_for_rails": self.counts_for_rails,
            "status": self.status, "exit_at": self.exit_at.isoformat() if self.exit_at else None, "exit_underlying": _r6(self.exit_spot),
            "exit_premium": _r6(self.exit_bid), "exit_reason": self.exit_reason, "exit_note": self.exit_note, "r_result": _r6(self.r_result),
            "proceeds": _r6(self.proceeds), "mfe_r": _r6(self.mfe_r), "mae_r": _r6(self.mae_r), "hwm_premium": _r6(self.hwm_bid),
            "trail_activated": self.trail_on, "trail_tightened": self.trail_tight, "banked_contracts": self.banked_contracts, "banked_proceeds": _r6(self.banked_proceeds),
            "banked_at": self.banked_at.isoformat() if self.banked_at else None, "marks": self.marks, "events": list(self.events),
            "model_version": "dxlink-marks-v1",
        }
