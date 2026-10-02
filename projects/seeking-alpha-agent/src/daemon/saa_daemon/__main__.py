"""CLI.

  python -m saa_daemon smoke [--no-telegram]         prove every leg works (any day, ~30 s)
  python -m saa_daemon session [--date YYYY-MM-DD] [--force]   run today's session and exit after the EOD report
  python -m saa_daemon forever                       run every trading day (VPS / systemd mode)
  python -m saa_daemon replay <recording.jsonl> [--engine] [--out ledger.json] [--eval]
                                                     rebuild bars from a recording; --engine runs the rules engine
                                                     deterministically and prints the ledger summary + sha256
  python -m saa_daemon kelly-table [--account 1000]  print the plan §0.3 table and the sizing grid
  python -m saa_daemon rules                         show the Tier 2 parameters the engine would run with (saa.rules latest)
  python -m saa_daemon check                         show .env readiness (key names only) and exit
  python -m saa_daemon load-econ <econ_calendar.json>  push the hand-maintained macro calendar into saa.calendar_days
  python -m saa_daemon paper-roundtrip [--symbol SPY] [--n 3] [--allow-delayed]   M4: sandbox round trip(s) through the ladder, reconciled
  python -m saa_daemon halt-test [--symbol SPY] [--allow-delayed]                 M4: open 1 contract, kill switch → flat within 10 s
  python -m saa_daemon approval-test [--timeout 180]                              M4: a real Telegram proposal (Approve / Skip / timeout = Skip)
  python -m saa_daemon halt [--reason ...] | resume                               engage / clear the kill switch (file flag + saa.settings.halt)
  python -m saa_daemon paper-status                                               today's paper book (SQLite)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .clock import Clock, Schedule, et, is_trading_day, next_trading_day
from .config import ConfigError, load_settings, readiness
from .log import setup_logging


def _settings(args: argparse.Namespace):
    try:
        return load_settings(args.env_file, state_dir=args.state_dir)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


async def _forever(settings) -> None:
    from .daemon import Daemon

    clock = Clock()
    log = logging.getLogger("saa")
    while True:
        today = et(clock.now()).date()
        target = today if is_trading_day(today) and clock.now() < Schedule.for_date(today).shutdown else next_trading_day(today)
        sched = Schedule.for_date(target)
        start = sched.prep - timedelta(minutes=5)
        if clock.now() < start:
            log.info("next session %s — sleeping until %s ET", target, et(start).strftime("%a %H:%M"))
            await clock.sleep_until(start)
        d = Daemon(settings, clock=clock, mode="forever", trade_date=target)
        try:
            res = await d.run_session()
            log.info("session %s done: ok=%s unhandled=%d", target, res.ok, res.unhandled)
        except Exception:  # noqa: BLE001 - the loop itself must survive anything
            log.exception("session %s crashed at the top level", target)
        await clock.sleep(120)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="saa-daemon", description=f"Seeking Alpha Agent daemon v{__version__}")
    p.add_argument("--env-file", help="path to .env (default: found automatically)")
    p.add_argument("--state-dir", help="SQLite/recordings/log directory (default: <daemon>/state)")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smoke")
    s.add_argument("--no-telegram", action="store_true")
    se = sub.add_parser("session")
    se.add_argument("--date", help="trade date YYYY-MM-DD (default today ET)")
    se.add_argument("--force", action="store_true", help="run even on a non-trading day / after hours (feed will be quiet)")
    sub.add_parser("forever")
    r = sub.add_parser("replay")
    r.add_argument("recording")
    r.add_argument("--engine", action="store_true", help="run the rules engine over the recording (deterministic)")
    r.add_argument("--out", help="write the canonical engine output (decisions + ledger) to this file")
    r.add_argument("--eval", action="store_true", help="evaluate entries even if the recording's feed was not real-time (offline study)")
    r.add_argument("--date", help="trade date for a pre-M3 recording without a Meta record")
    kt = sub.add_parser("kelly-table")
    kt.add_argument("--account", type=float, default=1000.0)
    kt.add_argument("--k", type=float, default=0.5)
    sub.add_parser("rules")
    sub.add_parser("check")
    le = sub.add_parser("load-econ")
    le.add_argument("path")
    for name in ("paper-roundtrip", "halt-test"):
        pp = sub.add_parser(name)
        pp.add_argument("--symbol", default="SPY")
        pp.add_argument("--allow-delayed", action="store_true", help="run on a non-real-time quote (sandbox plumbing only; recorded as such)")
        if name == "paper-roundtrip":
            pp.add_argument("--n", type=int, default=1)
    at = sub.add_parser("approval-test")
    at.add_argument("--timeout", type=float, default=180.0)
    h = sub.add_parser("halt")
    h.add_argument("--reason", default="")
    sub.add_parser("resume")
    sub.add_parser("paper-status")
    args = p.parse_args(argv)

    settings = _settings(args)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_level or settings.log_level, settings.secret_values(), settings.state_dir / "logs",
                  run_id=f"{args.cmd}-{date.today().isoformat()}")

    if args.cmd == "check":
        print(f".env: {settings.env_file or 'NOT FOUND'}")
        print(f"state dir: {settings.state_dir}")
        probs = readiness(settings)
        for k, v in settings.redacted().items():
            if k in ("env_file", "state_dir"):
                continue
            print(f"  {k} = {v}")
        print("READY for M2" if not probs else "NOT READY:\n  - " + "\n  - ".join(probs))
        return 0 if not probs else 1
    if args.cmd == "smoke":
        from .smoke import run_smoke
        return asyncio.run(run_smoke(settings, telegram=not args.no_telegram))
    if args.cmd == "session":
        from .daemon import Daemon
        d = Daemon(settings, mode="session", trade_date=date.fromisoformat(args.date) if args.date else None, force=args.force)
        res = asyncio.run(d.run_session())
        print(json.dumps({"run_id": res.run_id, "ok": res.ok, "unhandled": res.unhandled, "errors": res.errors,
                          "bars": res.stats.get("bars"), "snapshots": res.stats.get("snapshots"), "skipped": res.stats.get("skipped")}, indent=2, default=str))
        return 0 if res.ok else 1
    if args.cmd == "forever":
        asyncio.run(_forever(settings))
        return 0
    if args.cmd == "replay":
        from .replay import replay_bars
        if not args.engine:
            print(json.dumps(replay_bars(Path(args.recording)), indent=2, default=str))
            return 0
        from .engine.replay import replay_engine
        res = replay_engine(Path(args.recording), require_realtime=(False if args.eval else None),
                            trade_date=date.fromisoformat(args.date) if args.date else None)
        if args.out:
            Path(args.out).write_bytes(res.canonical)
        print(json.dumps(res.summary(), indent=2, default=str))
        return 0
    if args.cmd == "kelly-table":
        from .engine.kelly import format_table, kelly_table, plan_rows
        print("Plan §0.3 — f* = p − (1−p)/W, growth = p·ln(1+f·W) + (1−p)·ln(1−f):")
        print(format_table(plan_rows()))
        print()
        print(f"Sizing grid at account ${args.account:,.0f}, k = {args.k} (contracts at a given premium; known-edge posterior, caps + floor applied):")
        print(format_table(kelly_table(args.account, k=args.k)))
        return 0
    if args.cmd == "rules":
        from .engine.rules import Rules

        async def show() -> int:
            rules = Rules.default()
            if settings.mirror_configured and settings.mirror_enabled:
                from .http import Httpx2Client
                from .mirror import SupabaseMirror
                from .store import Store
                http = Httpx2Client()
                m = SupabaseMirror(settings, Store(settings.state_dir / "saa.sqlite"), http)
                try:
                    row = await m.rules_latest()
                    if row:
                        rules = Rules.from_row(row)
                finally:
                    await http.aclose()
            print(json.dumps({"version": rules.version, "evidence": rules.evidence, "ignored_tier1_keys": list(rules.ignored), "params": rules.params}, indent=2))
            return 0

        return asyncio.run(show())
    if args.cmd == "load-econ":
        from .http import Httpx2Client
        from .mirror import SupabaseMirror
        from .store import Store

        async def go() -> int:
            rows = json.loads(Path(args.path).read_text(encoding="utf-8"))
            rows = rows.get("events", rows) if isinstance(rows, dict) else rows
            http = Httpx2Client()
            m = SupabaseMirror(settings, Store(settings.state_dir / "saa.sqlite"), http)
            if not m.enabled:
                print("mirror not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")
                return 1
            n = await m.rpc("saa_econ_upsert", {"p_rows": rows})
            await http.aclose()
            print(f"econ calendar: {len(rows)} rows sent, {n} new events merged into saa.calendar_days")
            return 0

        return asyncio.run(go())
    if args.cmd in ("paper-roundtrip", "halt-test", "approval-test", "halt", "resume", "paper-status"):
        from .paper_cli import main_paper
        return main_paper(args.cmd, settings, args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
