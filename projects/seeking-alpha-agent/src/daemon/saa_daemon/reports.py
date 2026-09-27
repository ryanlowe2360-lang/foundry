"""Telegram texts: the 9:25 heartbeat and the post-close data report. Plain text, ≤ ~10 lines each."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .clock import et


def _f(v: Any, nd: int = 1) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _vix_line(term: dict[str, Any] | None) -> str:
    if not term:
        return "VIX: unavailable"
    shape = term.get("shape") or ""
    return f"VIX {_f(term.get('vix'))} · 1D {_f(term.get('vix1d'))} · 9D {_f(term.get('vix9d'))} · 3M {_f(term.get('vix3m'))}" + (f" ({shape})" if shape else "")


def _gamma_line(label: str, g: dict[str, Any] | None) -> str:
    if not g or g.get("regime") in (None, "unknown"):
        return f"Gamma {label}: unreadable"
    return (f"Gamma {label}: {g.get('regime')}, flip {_f(g.get('flip'), 2)}, call wall {g.get('call_wall')}, "
            f"put wall {g.get('put_wall')}, coverage {int(round((g.get('coverage') or 0) * 100))}%")


def heartbeat_text(ctx: dict[str, Any]) -> str:
    """ctx keys: trade_date, version, host, broker (SessionInfo-like dict), data (dict), universe (list), watch (list),
    chains (dict: n_index, n_single, exp_index, exp_single, n_options), vix (dict|None), gamma (dict|None, SPY pre-open),
    halts (int), econ (list[str]), mirror (bool), late (bool)."""
    b, d = ctx.get("broker", {}), ctx.get("data", {})
    broker = (f"sandbox ok (acct {b.get('account_masked') or '?'}, {b.get('account_type') or '?'}"
              f"{', options ' + str(b.get('options_level')) if b.get('options_level') else ''})") if b.get("ok") else f"sandbox FAILED ({b.get('error') or 'unknown'})"
    data = f"{d.get('env', 'prod')} DXLink ok" if d.get("ok") and d.get("quote_token_ok") else f"{d.get('env', 'prod')} FAILED ({d.get('error') or 'no quote token'})"
    idx, names = ctx.get("index_symbols", []), ctx.get("single_names", [])
    ch = ctx.get("chains", {})
    econ = ctx.get("econ") or []
    lines = [
        f"SAA daemon ▸ {ctx['trade_date']:%a %Y-%m-%d} · v{ctx.get('version', '?')} · {ctx.get('host', '?')}" + (" · LATE START" if ctx.get("late") else ""),
        f"Broker: {broker} | Data: {data}",
        f"Universe: {' '.join(idx)}" + (f" + {len(names)} names ({' '.join(names[:8])}{'…' if len(names) > 8 else ''})" if names else " (no single names in play)"),
        f"Chains: {ch.get('n_index', 0)} idx × {ch.get('exp_index', 0)} exp · {ch.get('n_single', 0)} names × {ch.get('exp_single', 0)} exp · {ch.get('n_options', 0)} option symbols",
        _vix_line(ctx.get("vix")),
        _gamma_line("SPY pre-open", ctx.get("gamma")),
        f"Halts so far: {ctx.get('halts', 0)} · Econ today: {'; '.join(econ) if econ else '—'}",
        f"Mirror: {'Supabase on' if ctx.get('mirror') else 'OFF (SQLite only)'} · no orders in M2",
    ]
    return "\n".join(lines)


def eod_text(stats: dict[str, Any]) -> str:
    """stats keys: trade_date, started, ended, unhandled, bars {sym: {complete, expected, missing, first_gap}},
    snapshots {und: n}, snapshots_expected, n_options, gamma_open, gamma_close, vix_open, vix_close, halts (list of dict),
    feed {events, reconnects, by_kind}, errors {task: n}, mirror {enabled, pushed, queue, failures}, telegram (list)."""
    bars = stats.get("bars", {})
    bar_bits = []
    for sym in sorted(bars):
        b = bars[sym]
        gap = f" (gap {et(datetime.fromisoformat(b['first_gap'].replace('Z', '+00:00'))):%H:%M})" if b.get("first_gap") else ""
        bar_bits.append(f"{sym} {b.get('complete', 0)}/{b.get('expected', 0)}{gap}")
    snaps = stats.get("snapshots", {})
    snap_n = sorted(set(snaps.values())) if snaps else []
    snap_desc = (f"{snap_n[0]}" if len(snap_n) == 1 else f"{snap_n[0]}–{snap_n[-1]}") if snap_n else "0"
    go, gc = stats.get("gamma_open") or {}, stats.get("gamma_close") or {}
    vo, vc = stats.get("vix_open") or {}, stats.get("vix_close") or {}
    halts = stats.get("halts") or []
    halt_desc = ", ".join(f"{h['symbol']} {et(datetime.fromisoformat(h['halt_time'].replace('Z', '+00:00'))):%H:%M} {h.get('reason_code') or ''}".strip()
                          for h in halts[:6]) + ("…" if len(halts) > 6 else "")
    feed = stats.get("feed", {})
    errs = stats.get("errors", {})
    err_desc = ", ".join(f"{k} {v}" for k, v in sorted(errs.items()) if v) or "none"
    m = stats.get("mirror", {})
    pushed = m.get("pushed", {})
    tg = stats.get("telegram") or []
    tg_desc = ", ".join(f"{t['kind']} via {t['path']}" for t in tg) if tg else "—"
    lines = [
        f"SAA daemon EOD ▸ {stats['trade_date']} · run {stats.get('started', '?')}–{stats.get('ended', '?')} ET · {stats.get('unhandled', 0)} unhandled exceptions",
        "Bars (1m): " + (" · ".join(bar_bits) if bar_bits else "none"),
        f"Chains: {snap_desc} snapshots × {len(snaps)} underlyings (expected {stats.get('snapshots_expected', '?')}) · {stats.get('n_options', 0)} option symbols",
        f"Gamma SPY: open {go.get('regime', '?')} (flip {_f(go.get('flip'), 2)}) → close {gc.get('regime', '?')} (flip {_f(gc.get('flip'), 2)})",
        f"VIX {_f(vo.get('vix'))}→{_f(vc.get('vix'))} · 1D {_f(vo.get('vix1d'))}→{_f(vc.get('vix1d'))} · 3M {_f(vo.get('vix3m'))}→{_f(vc.get('vix3m'))} · {vo.get('shape') or '?'}→{vc.get('shape') or '?'}",
        f"Halts: {len(halts)}" + (f" ({halt_desc})" if halts else ""),
        f"Feed: {feed.get('events', 0):,} events · {feed.get('reconnects', 0)} reconnects · errors caught: {err_desc}",
        f"Mirror: {'on' if m.get('enabled') else 'OFF'} · {pushed.get('bars', 0):,} bar upserts · {pushed.get('snapshots', 0)} snapshots · {pushed.get('vix', 0)} vix · queue {m.get('queue', 0)} · failures {m.get('failures', 0)}",
        f"Telegram: {tg_desc}",
    ]
    return "\n".join(lines)
