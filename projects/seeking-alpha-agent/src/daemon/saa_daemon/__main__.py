"""CLI.

  python -m saa_daemon smoke [--no-telegram]         prove every leg works (any day, ~30 s)
  python -m saa_daemon session [--date YYYY-MM-DD] [--force]   run today's session and exit after the EOD report
  python -m saa_daemon forever                       run every trading day (VPS / systemd mode)
  python -m saa_daemon replay <recording.jsonl>      rebuild bars from a recording, print stats
  python -m saa_daemon check                         show .env readiness (key names only) and exit
  python -m saa_daemon load-econ <econ_calendar.json>  push the hand-maintained macro calendar into saa.calendar_days
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
    sub.add_parser("check")
    le = sub.add_parser("load-econ")
    le.add_argument("path")
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
        print(json.dumps(replay_bars(Path(args.recording)), indent=2, default=str))
        return 0
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
