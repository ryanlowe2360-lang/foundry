"""`saa-daemon smoke` — a 30-second proof that every leg works from this machine, any day of the week:
.env → Supabase read → sandbox login → production login + quote token → spot REST → SPY chain → one DXLink
quote → Cboe VIX → Nasdaq halts → SQLite → Supabase write (saa.run_log 'daemon:smoke') → Telegram.
Prints PASS / WARN / FAIL per step and never prints a secret."""
from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .chains import from_sdk_nested, plan_chain
from .clock import et, is_trading_day
from .config import Settings, readiness
from .halts import fetch_halts
from .http import Httpx2Client, certifi_client
from .mirror import MirrorError, SupabaseMirror
from .store import Store
from .telegram import Notifier
from .vix import fetch_vix_term

log = logging.getLogger("saa.smoke")

CRITICAL = {"env", "sandbox_login", "prod_login", "quote_token", "chain", "supabase_write"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, step: str, status: str, detail: str = "") -> None:
        self.rows.append((step, status, detail))
        print(f"  [{status:4}] {step:<16} {detail}")

    @property
    def failed_critical(self) -> list[str]:
        return [s for s, st, _ in self.rows if st == "FAIL" and s in CRITICAL]


async def run_smoke(settings: Settings, *, telegram: bool = True) -> int:
    now = datetime.now(timezone.utc)
    print(f"SAA daemon v{__version__} smoke test — {et(now):%Y-%m-%d %H:%M} ET on {socket.gethostname().split('.')[0]}")
    rep = Report()
    probs = readiness(settings)
    rep.add("env", "PASS" if settings.env_file and not any(p.startswith("tastytrade") for p in probs) else "FAIL",
            f"{settings.env_file or 'no .env found'}" + (f" — {len(probs)} note(s)" if probs else ""))
    for p in probs:
        print(f"         note: {p}")

    http = Httpx2Client()
    store = Store(settings.state_dir / "saa.sqlite")
    mirror = SupabaseMirror(settings, store, http)
    detail: dict[str, Any] = {"host": socket.gethostname().split(".")[0], "version": __version__}

    # Supabase read
    if mirror.enabled:
        try:
            st = await mirror.rpc("saa_daemon_status", {})
            rep.add("supabase_read", "PASS", f"saa_daemon_status ok (bars today {st.get('bars_today')}, snapshots {st.get('snapshots_today')})")
        except MirrorError as e:
            rep.add("supabase_read", "FAIL", str(e))
    else:
        rep.add("supabase_read", "WARN", "mirror not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

    # tastytrade
    from .broker import Brokerage

    brk = Brokerage(settings)
    try:
        await brk.open()
    except Exception as e:  # noqa: BLE001
        rep.add("sandbox_login", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")
    bi, di = brk.broker_info, brk.data_info
    rep.add("sandbox_login", "PASS" if bi.ok else "FAIL",
            (f"account {bi.account_masked} ({bi.account_type}, options level {bi.options_level or '?'})" if bi.ok else bi.error or "failed"))
    rep.add("prod_login", "PASS" if di.ok else "FAIL", "production OAuth ok" if di.ok else (di.error or "failed"))
    rep.add("quote_token", "PASS" if di.quote_token_ok else "FAIL",
            (f"DXLink token issued (level {di.quote_level or '?'})") if di.quote_token_ok else (di.error or "no token"))
    detail.update(sandbox_ok=bi.ok, prod_ok=di.ok, quote_token_ok=di.quote_token_ok, account=bi.account_masked)

    plan = None
    spot = None
    if brk.data is not None:
        try:
            spots = await brk.spot_prices(list(settings.index_symbols))
            spot = spots.get(settings.index_symbols[0])
            rep.add("spot_rest", "PASS" if spots else "WARN", ", ".join(f"{k} {v:.2f}" for k, v in sorted(spots.items())) or "no prices returned")
        except Exception as e:  # noqa: BLE001
            rep.add("spot_rest", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")
        try:
            sym = settings.index_symbols[0]
            nested = await brk.nested_chain(sym)
            raw = from_sdk_nested(nested) if nested else []
            plan = plan_chain(sym, raw, spot or 0.0, today=et(now).date(), now_et=et(now).time(), n_exp=settings.expirations_index,
                              window_pct=settings.strike_window_pct_index, max_per_side=settings.max_strikes_per_side) if spot else None
            rep.add("chain", "PASS" if raw else "FAIL",
                    f"{sym}: {len(raw)} expirations" + (f", plan {len(plan.expirations)} exp / {plan.n_strikes} strikes / {len(plan.symbols())} symbols"
                                                        if plan else " (no spot → no plan)"))
            detail.update(chain_expirations=len(raw), plan_symbols=len(plan.symbols()) if plan else 0)
        except Exception as e:  # noqa: BLE001
            rep.add("chain", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")
        # one DXLink quote
        try:
            from tastytrade import DXLinkStreamer
            from tastytrade.dxfeed import Quote

            got: list[str] = []
            lags: list[float] = []
            async with DXLinkStreamer(brk.data) as st:
                from tastytrade.dxfeed import Trade

                subs = [settings.index_symbols[0]] + (plan.symbols()[:2] if plan else [])
                await st.subscribe(Quote, subs)
                await st.subscribe(Trade, [settings.index_symbols[0]])
                try:
                    async with asyncio.timeout(20):
                        while len(got) < min(2, len(subs)):
                            q = await st.get_event(Quote)
                            got.append(f"{q.event_symbol} {q.bid_price}/{q.ask_price}")
                except TimeoutError:
                    pass
                try:  # a few trades → exchange-time vs now = feed lag (only meaningful while the market trades)
                    async with asyncio.timeout(8):
                        while len(lags) < 5:
                            t = await st.get_event(Trade)
                            if t.time:
                                lags.append(datetime.now(timezone.utc).timestamp() - t.time / 1000.0)
                except TimeoutError:
                    pass
            if lags:
                lags.sort()
                med = lags[len(lags) // 2]
                detail["feed_lag_s"] = round(med, 1)
                detail["feed_lag_mode"] = "realtime" if med < 30 else "DELAYED"
                rep.add("feed_lag", "PASS" if med < 30 else "WARN", f"median {med:.0f}s between exchange time and receipt" +
                        (" — 15-MINUTE DELAYED DATA (account/entitlement, not the daemon)" if med > 600 else " — real-time" if med < 30 else ""))
            else:
                rep.add("feed_lag", "WARN", "no trades within 8 s (market closed?) — lag unmeasured")
            open_now = is_trading_day(et(now).date()) and et(now).hour * 60 + et(now).minute in range(9 * 60 + 30, 16 * 60)
            rep.add("dxlink", "PASS" if got else ("WARN" if not open_now else "FAIL"),
                    "; ".join(got) if got else "no quote within 20 s" + ("" if open_now else " (market closed — often normal)"))
            detail.update(dxlink_quotes=len(got))
        except Exception as e:  # noqa: BLE001
            rep.add("dxlink", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")

    # Cboe + Nasdaq
    try:
        term = await fetch_vix_term(http, now, fallback=certifi_client())
        ok = term.get("vix") is not None
        rep.add("cboe_vix", "PASS" if ok else "WARN", f"VIX {term.get('vix')} · 1D {term.get('vix1d')} · 9D {term.get('vix9d')} · 3M {term.get('vix3m')} ({term.get('shape')})"
                + (f" errors: {term['errors']}" if term.get("errors") else "") + (f" notes: {term['notes']}" if term.get("notes") else ""))
        if ok:
            store.insert_vix(now, term)
        detail["vix"] = {k: term.get(k) for k in ("vix", "vix1d", "vix9d", "vix3m")}
    except Exception as e:  # noqa: BLE001
        rep.add("cboe_vix", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")
    try:
        halts = await fetch_halts(http)
        rep.add("nasdaq_halts", "PASS", f"{len(halts)} halt rows in feed" + (f" (e.g. {halts[0]['symbol']} {halts[0]['reason_code']})" if halts else ""))
        if halts:
            store.upsert_halts(halts)
        detail["halts_in_feed"] = len(halts)
    except Exception as e:  # noqa: BLE001
        rep.add("nasdaq_halts", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")

    # SQLite
    try:
        run_id = f"smoke-{et(now):%Y%m%d-%H%M%S}"
        store.upsert_run(run_id, et(now).date().isoformat(), now, "done", detail, ended_at=datetime.now(timezone.utc))
        store.set_kv("last_smoke", now.isoformat())
        rep.add("sqlite", "PASS", str(store.path))
    except Exception as e:  # noqa: BLE001
        rep.add("sqlite", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")

    # Engine (M3): rules + checklists reachable, Kelly table reproduces the plan, the mark gate on the lag just measured
    try:
        from .engine.kelly import kelly_fraction, posterior
        from .engine.rules import Rules

        rules = Rules.default()
        n_cl = None
        if mirror.enabled:
            row = await mirror.rules_latest()
            if row:
                rules = Rules.from_row(row)
            n_cl = len(await mirror.checklists_today())
            hist = await mirror.engine_ledger(60)
        else:
            hist = []
        post = posterior([float(r["r_result"]) for r in hist if r.get("r_result") is not None])
        table_ok = [round(kelly_fraction(p_, w_) * 100) for p_, w_ in ((0.25, 4), (0.30, 5), (0.35, 6))] == [6, 16, 24]
        lag_mode = detail.get("feed_lag_mode") or "unknown"
        rep.add("engine", "PASS" if table_ok else "FAIL",
                f"rules v{rules.version}" + (f" ({len(rules.ignored)} Tier 1 keys ignored)" if rules.ignored else "") +
                (f" · {n_cl} checklists today" if n_cl is not None else " · checklists n/a (mirror off)") +
                f" · ledger n={post.n} p={post.p:.3f} W={post.w:.1f} f*={post.f_full:.3f}" + (" → floor" if post.n == 0 else "") +
                f" · Kelly table {'ok' if table_ok else 'MISMATCH'} · live eval {'ON' if lag_mode == 'realtime' else 'OFF (feed ' + lag_mode + ')'}")
        detail.update(rules_version=rules.version, engine_posterior_n=post.n)
    except Exception as e:  # noqa: BLE001
        rep.add("engine", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")

    # Supabase write
    if mirror.enabled:
        try:
            await mirror.rpc("saa_daemon_run", {"p_run_id": run_id, "p_patch": {"mode": "smoke", "status": "done", "host": detail["host"],
                                                                                "version": __version__, "stats": detail, "ended_at": datetime.now(timezone.utc).isoformat()}})
            await mirror.rpc("saa_log_run", {"p_job": "daemon:smoke", "p_ok": not rep.failed_critical, "p_detail": detail})
            await mirror.flush_all()
            rep.add("supabase_write", "PASS", "saa.daemon_runs + saa.run_log 'daemon:smoke' written; VIX/halts mirrored")
        except MirrorError as e:
            rep.add("supabase_write", "FAIL", str(e))
    else:
        rep.add("supabase_write", "WARN", "skipped — mirror not configured")

    # Telegram
    if telegram:
        try:
            n = Notifier(settings, mirror, http)
            crit = rep.failed_critical
            text = (f"SAA daemon smoke {'OK' if not crit else 'FAILED (' + ', '.join(crit) + ')'} · {et(now):%a %H:%M} ET · v{__version__} on {detail['host']}\n"
                    f"sandbox {bi.account_masked or 'x'} · data {'ok' if di.quote_token_ok else 'x'} · chain {detail.get('plan_symbols', 0)} syms · "
                    f"VIX {detail.get('vix', {}).get('vix')} · mirror {'on' if mirror.enabled else 'off'}")
            path = await n.send("test", text, now)
            rep.add("telegram", "PASS" if path in ("outbox", "telegram_direct") else "WARN", f"via {path}")
        except Exception as e:  # noqa: BLE001
            rep.add("telegram", "FAIL", f"{type(e).__name__}: {str(e)[:120]}")
    else:
        rep.add("telegram", "WARN", "skipped (--no-telegram)")

    await brk.close()
    await http.aclose()
    store.close()
    crit = rep.failed_critical
    print("\nRESULT:", "ALL CRITICAL STEPS PASSED" if not crit else f"FAILED: {', '.join(crit)}")
    return 0 if not crit else 1
