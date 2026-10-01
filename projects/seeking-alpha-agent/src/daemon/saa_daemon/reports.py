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
    if d.get("quote_level"):
        data += f" (token level {d['quote_level']})"
    lag = ctx.get("lag") or {}
    if lag.get("lag_s") is not None:
        data += f" · feed lag {lag['lag_s']:.0f}s" + (" ⚠ DELAYED DATA" if lag.get("mode") == "DELAYED" else " (real-time)")
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
        _engine_heartbeat_line(ctx.get("engine"), lag),
        f"Mirror: {'Supabase on' if ctx.get('mirror') else 'OFF (SQLite only)'} · shadow trades only, no orders (M3)",
    ]
    return "\n".join(lines)


def _engine_heartbeat_line(eng: dict[str, Any] | None, lag: dict[str, Any]) -> str:
    """engine state → one line: rules version, account, k, posterior, size mode, rails, feed gate."""
    if not eng:
        return "Engine: not loaded"
    post, rails, rules = eng.get("posterior") or {}, eng.get("rails") or {}, eng.get("rules") or {}
    n = post.get("n", 0)
    if not eng.get("require_realtime") or lag.get("mode") == "realtime":
        mode = "LIVE eval"
    elif lag.get("mode") == "DELAYED":
        mode = f"observe-only (feed DELAYED {lag.get('lag_s', 0):.0f}s)"
    else:
        mode = "feed lag not measured yet (real-time required to evaluate)"
    halt = rails.get("halt_mode", "none")
    bits = [f"Engine: {mode}", f"rules v{rules.get('version', '?')}", f"acct ${eng.get('account', 0):,.0f} k={eng.get('kelly_k', 0):.2f}",
            f"posterior n={n} p={post.get('p', 0):.3f} W={post.get('w', 0):.1f} f*={post.get('f_full', 0):.3f}" + (" → floor" if n == 0 else ""),
            f"halt {halt}" if halt != "none" else "halt none", "cooling-off ½ size" if rails.get("cooling_off_today") else "",
            f"{len(rules.get('lanes_on') or [])} fast lanes"]
    return " · ".join(b for b in bits if b)


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
        f"Feed: {feed.get('events', 0):,} events · {feed.get('reconnects', 0)} reconnects · lag {_f(feed.get('lag_s'), 0)}s"
        + (" ⚠ DELAYED DATA" if feed.get("mode") == "DELAYED" else (" real-time" if feed.get("mode") == "realtime" else "")) + f" · errors caught: {err_desc}",
        f"Mirror: {'on' if m.get('enabled') else 'OFF'} · {pushed.get('bars', 0):,} bar upserts · {pushed.get('snapshots', 0)} snapshots · {pushed.get('vix', 0)} vix"
        + (f" · {pushed.get('engine_trades', 0)} ledger rows" if pushed.get("engine_trades") else "") + f" · queue {m.get('queue', 0)} · failures {m.get('failures', 0)}",
        f"Telegram: {tg_desc}",
    ]
    lines[7:7] = _engine_eod_lines(stats.get("engine"), feed)
    return "\n".join(lines)


def _engine_eod_lines(eng: dict[str, Any] | None, feed: dict[str, Any]) -> list[str]:
    if not eng:
        return ["Engine: not loaded"]
    c, pos, rails, post = eng.get("counts") or {}, eng.get("positions") or {}, eng.get("rails") or {}, eng.get("posterior") or {}
    sd = eng.get("stand_down_reasons") or {}
    top = ", ".join(f"{k} {v}" for k, v in sorted(sd.items(), key=lambda kv: -kv[1])[:4]) or "none"
    observe, entry_ticks = eng.get("observe_only_ticks", 0), eng.get("entry_ticks", 0)
    gate_mode = (f"OBSERVE-ONLY all day (feed {feed.get('mode')})" if observe and entry_ticks and observe >= entry_ticks
                 else (f"observe-only {observe}/{entry_ticks} ticks" if observe else "live eval"))
    day = rails.get("day") or {}
    lines = [
        f"Engine: rules v{(eng.get('rules') or {}).get('version', '?')} · {gate_mode} · {c.get('evaluations', 0)} gate evaluations · {c.get('fired', 0)} fired · "
        f"{c.get('fast_lane_opens', 0)} fast-lane shadows · {c.get('closes', 0)} closed · {pos.get('void', 0)} void",
        f"Shadow R: gate {eng.get('today_gate_r', 0):+.2f}R ({day.get('wins', 0)}W/{day.get('losses', 0)}L) · fast-lane {eng.get('today_fast_lane_r', 0):+.2f}R · "
        f"posterior n={post.get('n', 0)} p={post.get('p', 0):.3f} W={post.get('w', 0):.1f} · halt {rails.get('halt_mode', 'none')}"
        + (" · cooling-off next session" if rails.get("cooling_off_triggered") else ""),
        f"Stand-downs: {top}",
    ]
    return lines
