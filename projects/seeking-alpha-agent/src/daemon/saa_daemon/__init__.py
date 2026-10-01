"""Seeking Alpha Agent — trading daemon (M2 data plane + M3 rules engine).

Runs on Ryan's Mac (M2–M4) or the VPS (M5+). Reads `.env` from the Seeking Alpha Agent folder,
logs into tastytrade (sandbox account + production market data), streams DXLink quotes / greeks /
1-minute candles, snapshots option chains every 5 minutes, polls Nasdaq halts and the Cboe VIX
term structure, keeps hot state in SQLite and mirrors everything into the Supabase `saa` schema.

M3 adds the rules engine (`saa_daemon.engine`): six gates, window scheduler, Tier 1 rails, fractional Kelly with
shrinkage, and a shadow ledger marked at bid from DXLink — ticking once a minute on a recorded mark digest so every
session replays deterministically. Live evaluation is gated on a real-time feed.

M2/M3 place no orders. There is no order code in this package.
"""

__version__ = "0.3.0"
