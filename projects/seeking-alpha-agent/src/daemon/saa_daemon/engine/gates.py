"""The six-gate decision, STAND-DOWN default (corpus mode-b-daily-decision-framework §3; plan §2/§3).

1. dated catalyst · 2. regime readable · 3. regime favorable · 4. wedge present · 5. direction known · 6. trigger lands in a
documented high-odds window. All six must pass to FIRE. Gates 1 and 5 (and 3–4 for single names) come from the 7:40
brief's checklist row for the symbol (Claude's Tier 3 judgment, Brier-scored); 2–4 are measured natively where the
daemon can (dealer-gamma proxy, chain coverage, realized vol vs ATM IV); 6 is a native bar trigger or, in an event
window, the first post-release bar closing in the thesis direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..bars import Bar
from .rules import Rules
from .triggers import Signal, realized_vol_annualized
from .windows import Window

GATE_NAMES = ("catalyst", "readable", "favorable", "wedge", "direction", "trigger")


@dataclass(frozen=True)
class ChainRead:
    """What the mark digest says about the symbol's chain right now."""
    coverage_quotes: float      # share of planned near-ATM options with a two-sided quote
    coverage_greeks: float
    atm_iv: float | None        # annualized ATM implied vol (nearest expiration)
    n_options: int


@dataclass
class GateResult:
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    direction: str = "none"            # long | short | two_sided | none
    probability: float | None = None
    checklist_id: int | None = None
    target_r: float | None = None
    structure: str | None = None
    trigger: Signal | None = None

    @property
    def passed(self) -> int:
        return sum(1 for g in self.gates.values() if g.get("pass"))

    @property
    def all_pass(self) -> bool:
        return self.passed == len(GATE_NAMES)

    def failed(self) -> list[str]:
        return [k for k in GATE_NAMES if not self.gates.get(k, {}).get("pass")]

    def as_dict(self) -> dict[str, Any]:
        return {"gates": {k: self.gates.get(k, {"pass": False, "note": "not evaluated"}) for k in GATE_NAMES}, "passed": self.passed,
                "all_pass": self.all_pass, "direction": self.direction, "probability": self.probability, "checklist_id": self.checklist_id,
                "target_r": self.target_r, "structure": self.structure, "trigger": self.trigger.as_dict() if self.trigger else None}


def _g(ok: bool, note: str, source: str) -> dict[str, Any]:
    return {"pass": bool(ok), "note": note, "source": source}


def _cl_gate(cl: dict[str, Any] | None, key: str) -> bool | None:
    if not cl:
        return None
    g = cl.get("gates") or {}
    v = g.get(key)
    return None if v is None else bool(v)


def evaluate_gates(*, symbol: str, is_index: bool, now: datetime, window: Window, checklist: dict[str, Any] | None,
                   gamma: dict[str, Any] | None, vix: dict[str, Any] | None, chain: ChainRead | None, bars: Sequence[Bar],
                   signals: Sequence[Signal], rules: Rules, release_passed: bool = False) -> GateResult:
    res = GateResult()
    gr = rules.gates
    min_cov = float(gr["readable_min_coverage"])

    # 1. dated catalyst --------------------------------------------------------------------------------------------
    cl_cat = _cl_gate(checklist, "catalyst")
    if cl_cat:
        res.gates["catalyst"] = _g(True, checklist.get("catalyst") or "brief: dated catalyst", "brief")
    elif window.kind in ("event", "presser") and is_index:
        res.gates["catalyst"] = _g(True, f"scheduled release in session: {window.event}", "calendar")
    elif checklist is None:
        res.gates["catalyst"] = _g(False, "no checklist for this symbol today" if gr.get("require_checklist", True) else "no checklist", "brief")
    else:
        res.gates["catalyst"] = _g(False, "brief: no dated catalyst", "brief")

    # 2. regime readable ---------------------------------------------------------------------------------------------
    regime = (gamma or {}).get("regime")
    cov = float((gamma or {}).get("coverage") or 0.0)
    gamma_readable = regime in ("positive", "negative") and cov >= min_cov
    if is_index:
        vix_ok = bool(vix and vix.get("vix") is not None)
        ok = gamma_readable and vix_ok
        note = f"gamma {regime or 'unknown'} (coverage {cov:.0%}), VIX {'ok' if vix_ok else 'missing'}"
        res.gates["readable"] = _g(ok, note, "daemon")
    else:
        chain_ok = chain is not None and chain.coverage_quotes >= min_cov and chain.coverage_greeks >= min_cov
        brief_readable = _cl_gate(checklist, "readable")
        ok = chain_ok and (gamma_readable or bool(brief_readable))
        note = (f"chain coverage quotes {chain.coverage_quotes:.0%} / greeks {chain.coverage_greeks:.0%}" if chain else "no chain read") + \
               f"; gamma {regime or 'unknown'}" + ("" if brief_readable is None else f"; brief readable={brief_readable}")
        res.gates["readable"] = _g(ok, note, "daemon+brief")

    # 3. regime favorable --------------------------------------------------------------------------------------------
    if is_index:
        want = gr["favorable_index_regime"]
        ok = regime == want
        res.gates["favorable"] = _g(ok, f"index window needs dealer gamma {want}; proxy says {regime or 'unknown'}", "daemon")
    else:
        bf = _cl_gate(checklist, "favorable")
        res.gates["favorable"] = _g(bool(bf), f"brief favorable={bf}" + (f"; own gamma {regime}" if regime else ""), "brief")

    # 4. wedge -------------------------------------------------------------------------------------------------------
    rv = realized_vol_annualized(bars, int(gr["wedge_rv_minutes"]))
    iv = chain.atm_iv if chain else None
    if rv is not None and iv:
        ratio = rv / iv
        ok = ratio > float(gr["wedge_rv_over_iv"])
        res.gates["wedge"] = _g(ok, f"RV({gr['wedge_rv_minutes']}m) {rv:.1%} vs ATM IV {iv:.1%} → {ratio:.2f}× (need > {gr['wedge_rv_over_iv']})", "daemon")
    elif gr.get("use_brief_wedge", True) and _cl_gate(checklist, "wedge") is not None:
        bw = _cl_gate(checklist, "wedge")
        res.gates["wedge"] = _g(bool(bw), f"brief wedge={bw} (RV not measurable yet)", "brief")
    else:
        res.gates["wedge"] = _g(False, "no realized-vol sample and no brief wedge", "daemon")

    # 5. direction ---------------------------------------------------------------------------------------------------
    d = (checklist or {}).get("direction") or "none"
    if d in ("long", "short"):
        res.gates["direction"] = _g(True, f"brief: {d}", "brief")
    elif d == "two_sided":
        res.gates["direction"] = _g(True, "brief: magnitude only → small two-sided structure", "brief")
    else:
        res.gates["direction"] = _g(False, "direction unknown (magnitude-only without a two-sided plan, or no checklist)", "brief")
    res.direction = d if d in ("long", "short", "two_sided") else "none"
    if checklist:
        res.probability = checklist.get("probability")
        res.checklist_id = checklist.get("id")
        res.target_r = checklist.get("target_r")
        res.structure = checklist.get("structure")

    # 6. trigger -----------------------------------------------------------------------------------------------------
    trig: Signal | None = None
    if window.kind in ("event", "presser"):
        if release_passed and bars and res.direction in ("long", "short", "two_sided"):
            last = bars[-1]
            move = (last.close or 0) - (last.open or last.close or 0)
            dir_ok = res.direction == "two_sided" or (move > 0 if res.direction == "long" else move < 0)
            if dir_ok and move != 0:
                trig = Signal(window.kind, "long" if move > 0 else "short", float(last.close), last.start, f"first post-release bar {move:+.2f} holds")
        res.gates["trigger"] = _g(trig is not None, trig.note if trig else "post-release bar not yet in the thesis direction", "daemon")
    else:
        for s in signals:
            if res.direction == "two_sided" or s.direction == res.direction:
                trig = s
                break
        res.gates["trigger"] = _g(trig is not None, f"{trig.lane}: {trig.note}" if trig else ("no native trigger on the last bar" if signals == [] or signals == () else
                                                                                           f"trigger against thesis ({', '.join(f'{s.lane} {s.direction}' for s in signals)})"), "daemon")
    res.trigger = trig
    if trig is not None and res.direction == "two_sided":
        pass  # both legs open; the trigger only confirms magnitude
    return res
