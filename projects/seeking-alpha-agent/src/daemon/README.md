# Seeking Alpha Agent — daemon (M2: data plane) v0.2.0

The long-running process that will eventually place the trades. In M2 it **places no orders**; it
proves the data plane: broker logins, the DXLink stream, 1-minute bars, 5-minute option-chain
snapshots with an OI-based dealer-gamma proxy, Nasdaq halts, the Cboe VIX term structure, SQLite hot
state, a mirror into Supabase `saa.*`, and the 9:25 heartbeat / 4:20 report on Telegram.

Source of truth: Foundry `projects/seeking-alpha-agent/src/daemon/` (this folder). Deployed copy:
`Desktop/Seeking Alpha Agent /agent/daemon/` on Ryan's Mac (M2–M4), `/opt/saa/agent/daemon/` on the VPS (M5+).

## Run it

```bash
cd "$HOME/Desktop/Seeking Alpha Agent /agent/daemon"
./run.sh check      # .env readiness — key names only, never values
./run.sh smoke      # ~30 s: sandbox login, production login + DXLink token, spot REST, SPY chain,
                    #        one live quote, Cboe VIX, Nasdaq halts, SQLite, Supabase write, Telegram
./run.sh session    # today's session; exits after the 16:20 report (start any time before 16:25 ET)
```

`run.sh` needs Python 3.11+ (`brew install python@3.12` if missing); the first run creates `.venv/`
and installs `requirements.txt`. Everything it writes lives under `state/`:
`state/saa.sqlite` (hot state), `state/recordings/<run>.jsonl` (underlying-level events, for replay),
`state/logs/`.

Scheduled: `deploy/com.saa.daemon.plist` (launchd, 09:10 ET weekdays — the Mac must be awake) or
`deploy/saa-daemon.service` (systemd, `forever` mode) — instructions in each file's header.

## `.env` keys (in the Seeking Alpha Agent folder, one level above `agent/`)

| Key | Used for | Required in M2 |
|---|---|---|
| `TT_PROD_CLIENT_ID` / `TT_PROD_CLIENT_SECRET` / `TT_PROD_REFRESH_TOKEN` | **market data** (DXLink, chains, REST quotes) — read-only | yes |
| `TT_SANDBOX_CLIENT_ID` / `TT_SANDBOX_CLIENT_SECRET` / `TT_SANDBOX_REFRESH_TOKEN` | the paper account (M4 orders); M2 only reads it | yes |
| `SUPABASE_URL` | `https://zspbkcheounkwnpjkgrv.supabase.co` | yes (mirror) |
| `SUPABASE_SERVICE_ROLE_KEY` | writes to `saa.*` through the `public.saa_*` RPCs | yes (mirror) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | **fallback** delivery only; the primary path is `saa.outbox` | optional |
| `SAA_DATA_ENV` | `prod` (default) or `sandbox` — the sandbox has no market data (D11) | optional |
| `SAA_INDEX_SYMBOLS` | overrides `saa.settings.index_symbols` (default SPY,QQQ,IWM) | optional |
| `SAA_EXTRA_SYMBOLS`, `SAA_MAX_SINGLE_NAMES` (25), `SAA_STRIKE_WINDOW_PCT_INDEX` (3), `SAA_STRIKE_WINDOW_PCT_SINGLE` (8), `SAA_MAX_STRIKES_PER_SIDE` (20), `SAA_EXPIRATIONS_INDEX` (2), `SAA_EXPIRATIONS_SINGLE` (1), `SAA_SNAPSHOT_MINUTES` (5), `SAA_RECORD_EVENTS` (true), `SAA_MIRROR` (true), `SAA_LOG_LEVEL` (INFO), `SAA_STATE_DIR` | tuning | optional |

`SAA_BROKER_ENV` is fixed to `sandbox`; setting anything else is a configuration error until M5.
Secrets are wrapped so they cannot appear in logs or reports; any known secret value is redacted from
every log line as a second guard.

## What a session does (ET)

| When | What |
|---|---|
| start → 09:20 | wait (a late start joins in progress) |
| 09:20 | log in (production data + sandbox account), read `saa.settings.index_symbols`, `saa_active_symbols()` (brief watch list, triggers, open shadows) capped at 25 names, spot prices, nested chains → strike windows (indices ±3 %, 2 expirations; names ±8 %, 1 expiration; ≤20 strikes per side), subscribe DXLink: underlying Quote/Trade/Summary/Profile, 1-minute Candles from 09:30, option Quote/Greeks/Summary/Trade in chunks of 150 |
| 09:25 | Telegram heartbeat (via `saa.outbox`) + the pre-open baseline snapshot |
| 09:30–16:00 | bars flushed to SQLite every 5 s; chain snapshot per underlying every 5 min (compact strike arrays + summary + gamma proxy); Cboe VIX term every 5 min; Nasdaq halts RSS every 60 s; watch list re-read every 5 min (new names get chains + subscriptions); Supabase mirror every 10 s; machine heartbeat (`saa_daemon_run`, `saa.settings.daemon_last_seen`) every 60 s |
| 16:20 | Telegram EOD data report; `saa.run_log` row `daemon:session` with bar counts, snapshot counts, errors |
| 16:25 | exit |

A websocket drop raises inside the feed task; the supervisor restarts it with backoff and dxfeed
replays the day's candles, which the bar book upserts idempotently. Every task is supervised: the
acceptance criterion is zero *unhandled* exceptions, and the EOD report lists the caught ones.

## Where the data lands

| SQLite (`state/saa.sqlite`) | Supabase (`saa.*`) via RPC |
|---|---|
| `bars_1m` | `saa.bars_1m` (`saa_bars_upsert`) |
| `chain_snapshots` | `saa.chain_snapshots` (`saa_chain_snapshot`) |
| `vix_term` | `saa.market_snapshots` kind `vix_term` (`saa_snapshot`) |
| `halts` | `saa.halts` (`saa_halts_upsert`) |
| `runs`, `events_log` | `saa.daemon_runs` (`saa_daemon_run`), `saa.run_log` (`saa_log_run`) |
| `mirror_queue` | durable retry queue for outbox / run_log calls while Supabase is unreachable |

Chain snapshot strike rows are arrays in this order:
`[strike, call_bid, call_ask, call_iv, call_delta, call_gamma, call_oi, call_volume, put_bid, put_ask, put_iv, put_delta, put_gamma, put_oi, put_volume]`.

Dealer-gamma proxy per snapshot: `net_gex / call_gex / put_gex` (dollar gamma per 1 % move =
gamma × OI × 100 × spot² × 0.01, calls +, puts −), `flip` (cumulative-GEX zero crossing), `call_wall`,
`put_wall`, `regime`, `coverage`. It is a recorded proxy for the ledger to test, not a trading rule.

## Tests

From `projects/seeking-alpha-agent`: `python3 -m pytest src/tests -q` (28 tests; needs `pip install -r
src/daemon/requirements-dev.txt`). `test_daemon_session_sim.py` runs a whole trading day through the
real daemon loop on a virtual clock with fake broker/feed/PostgREST: ≥380 bars per symbol, 80
snapshots per underlying, a reconnect at 11:00, zero unhandled exceptions, heartbeat + EOD via outbox.
