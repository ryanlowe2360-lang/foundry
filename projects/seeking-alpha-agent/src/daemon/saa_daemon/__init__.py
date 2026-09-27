"""Seeking Alpha Agent — trading daemon (M2: data plane).

Runs on Ryan's Mac (M2–M4) or the VPS (M5+). Reads `.env` from the Seeking Alpha Agent folder,
logs into tastytrade (sandbox account + production market data), streams DXLink quotes / greeks /
1-minute candles, snapshots option chains every 5 minutes, polls Nasdaq halts and the Cboe VIX
term structure, keeps hot state in SQLite and mirrors everything into the Supabase `saa` schema.

M2 places no orders. There is no order code in this package.
"""

__version__ = "0.2.0"
