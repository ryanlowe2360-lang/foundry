"""Tier 2 — the learned parameters, read from the versioned `saa.rules` table (latest version).

`Rules.from_params()` accepts the JSON `params` of a rules row (v1 from intake or v2+ from the M3 build / Friday
reviews), fills every missing key with the built-in default, validates ranges, and *refuses* Tier 1 keys (see
tier1.TIER1_KEYS_IN_RULES): those are recorded in `ignored` so the heartbeat can say a rules row tried to carry them.
Defaults equal the v1 seed where v1 had the key, otherwise the plan / corpus placeholder values. All times are ET
wall-clock strings ("HH:MM").
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import time
from typing import Any

from .tier1 import TIER1_KEYS_IN_RULES

log = logging.getLogger("saa.engine.rules")

DEFAULT_PARAMS: dict[str, Any] = {
    "arming_thresholds": {"partial": 15, "armed": 28, "max": 56},
    "partial_bank_frac_of_account": 0.075,
    "trail": {"frac": 0.30, "tight_frac": 0.20, "tight_at_r": 3.0, "atr_ticks": 10},
    "shadow": {"strike_sigma_otm": 0.5, "contracts": 1, "min_spread_abs": 0.02, "single_name_default_sigma_d": 0.025,
               "last_entry_et": "15:30", "mark_window_pct": 1.5},
    "windows": {
        "open": {"start": "09:30", "entry_until": "09:58", "stop": "10:00", "data_day_entry_from": "09:35"},
        "mid": {"start": "10:00", "entry_until": "11:30", "stop": "11:30"},            # fresh entries only against a named trigger
        "afternoon": {"start": "13:30", "entry_until": "15:00", "stop": "15:00"},      # event windows only
        "last_hour": {"start": "15:00", "entry_until": "15:40", "stop": "15:55", "require_negative_gamma": True},
        "event": {"entry_after_min": 5, "entry_until_min": 15, "stop_after_min": 55, "presser_offset_min": 30,
                  "presser_entry_min": 15, "presser_stop_min": 60},
    },
    "fast_lanes": {
        "orb": {"enabled": True, "opens_shadow": True, "entry_until_et": "10:30", "range_minutes": 5, "volume_mult": 1.5},
        "vwap": {"enabled": True, "opens_shadow": True, "entry_until_et": "15:30", "min_bars": 15},
        "continuation": {"enabled": True, "opens_shadow": True, "entry_until_et": "11:30", "start_et": "10:00", "volume_mult": 1.2, "lookback": 10},
        "rvol": {"enabled": True, "opens_shadow": True, "entry_until_et": "11:30", "mult": 2.0},
        "compression": {"enabled": True, "opens_shadow": False},
    },
    "gates": {"readable_min_coverage": 0.5, "favorable_index_regime": "negative", "wedge_rv_minutes": 60,
              "wedge_rv_over_iv": 1.0, "use_brief_wedge": True, "require_checklist": True},
    "exits": {"vwap_loss": {"enabled": True, "bars": 1, "single_names_only": True},
              "volume_taper": {"enabled": True, "bars": 3, "frac": 0.5},
              "failed_extreme": {"enabled": True}},
    "max_concurrent_positions": 3,
    "max_alerts_per_day": 20,
    "promotion_gate": {"min_shadow_trades": 60, "expectancy_gt": 0},
    "tier2_change_policy": {"min_relevant_trades": 30, "max_changes_per_week": 1},
}


class RulesError(ValueError):
    pass


def parse_hhmm(s: str) -> time:
    try:
        hh, mm = s.strip().split(":")
        return time(int(hh), int(mm))
    except Exception as e:  # noqa: BLE001
        raise RulesError(f"bad time {s!r} (want HH:MM)") from e


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass(frozen=True)
class Rules:
    version: int
    params: dict[str, Any]
    ignored: tuple[str, ...] = ()          # Tier 1 keys the row tried to carry
    evidence: str | None = None
    effective_from: str | None = None

    # ------------------------------------------------------------------ constructors
    @classmethod
    def default(cls) -> "Rules":
        return cls(version=0, params=copy.deepcopy(DEFAULT_PARAMS), evidence="built-in defaults (mirror off or rules unreadable)")

    @classmethod
    def from_params(cls, params: dict[str, Any] | None, version: int, *, evidence: str | None = None,
                    effective_from: str | None = None) -> "Rules":
        params = params or {}
        ignored = tuple(sorted(k for k in params if k in TIER1_KEYS_IN_RULES))
        clean = {k: v for k, v in params.items() if k not in TIER1_KEYS_IN_RULES}
        merged = _merge(DEFAULT_PARAMS, clean)
        r = cls(version=int(version), params=merged, ignored=ignored, evidence=evidence, effective_from=effective_from)
        r.validate()
        if ignored:
            log.warning("rules v%d carries Tier 1 keys %s — ignored (Tier 1 is code)", version, ", ".join(ignored))
        return r

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "Rules":
        """A `saa.rules` row as returned by saa_rules_latest() ({version, params, evidence, effective_from})."""
        if not row or not isinstance(row.get("params"), dict):
            return cls.default()
        return cls.from_params(row["params"], int(row.get("version") or 0), evidence=row.get("evidence"),
                               effective_from=str(row.get("effective_from")) if row.get("effective_from") else None)

    # ------------------------------------------------------------------- validation
    def validate(self) -> None:
        p = self.params
        bank = float(p["partial_bank_frac_of_account"])
        if not 0 < bank < 1:
            raise RulesError("partial_bank_frac_of_account must be in (0, 1)")
        t = p["trail"]
        if not (0 < float(t["frac"]) < 1 and 0 < float(t["tight_frac"]) <= float(t["frac"]) and float(t["tight_at_r"]) > 0 and int(t["atr_ticks"]) >= 1):
            raise RulesError("trail parameters out of range")
        sh = p["shadow"]
        if not (0 <= float(sh["strike_sigma_otm"]) <= 3 and int(sh["contracts"]) >= 1 and float(sh["min_spread_abs"]) >= 0):
            raise RulesError("shadow parameters out of range")
        parse_hhmm(sh["last_entry_et"])
        for name, w in p["windows"].items():
            if name == "event":
                if not (0 <= int(w["entry_after_min"]) < int(w["entry_until_min"]) < int(w["stop_after_min"])):
                    raise RulesError("event window minutes must be ordered: after < until < stop")
                continue
            s, u, e = parse_hhmm(w["start"]), parse_hhmm(w["entry_until"]), parse_hhmm(w["stop"])
            if not (s <= u <= e):
                raise RulesError(f"window {name}: start ≤ entry_until ≤ stop required")
        for lane, cfg in p["fast_lanes"].items():
            if "entry_until_et" in cfg:
                parse_hhmm(cfg["entry_until_et"])
            if "start_et" in cfg:
                parse_hhmm(cfg["start_et"])
        g = p["gates"]
        if not 0 <= float(g["readable_min_coverage"]) <= 1:
            raise RulesError("gates.readable_min_coverage must be in [0, 1]")
        if int(p["max_concurrent_positions"]) < 1:
            raise RulesError("max_concurrent_positions must be ≥ 1")

    # -------------------------------------------------------------------- accessors
    @property
    def bank_frac(self) -> float:
        return float(self.params["partial_bank_frac_of_account"])

    @property
    def trail(self) -> dict[str, Any]:
        return self.params["trail"]

    @property
    def shadow(self) -> dict[str, Any]:
        return self.params["shadow"]

    @property
    def windows(self) -> dict[str, Any]:
        return self.params["windows"]

    @property
    def fast_lanes(self) -> dict[str, Any]:
        return self.params["fast_lanes"]

    @property
    def gates(self) -> dict[str, Any]:
        return self.params["gates"]

    @property
    def exits(self) -> dict[str, Any]:
        return self.params["exits"]

    @property
    def max_concurrent(self) -> int:
        return int(self.params["max_concurrent_positions"])

    @property
    def max_alerts_per_day(self) -> int:
        return int(self.params["max_alerts_per_day"])

    def lane(self, name: str) -> dict[str, Any] | None:
        return self.fast_lanes.get(name)

    def summary(self) -> dict[str, Any]:
        return {"version": self.version, "bank_frac": self.bank_frac, "trail": dict(self.trail), "strike_sigma_otm": self.shadow["strike_sigma_otm"],
                "max_concurrent": self.max_concurrent, "ignored_tier1_keys": list(self.ignored),
                "lanes_on": sorted(k for k, v in self.fast_lanes.items() if v.get("enabled") and v.get("opens_shadow"))}
