# Seeking Alpha Agent — daemon (M2 data plane + M3 rules engine + M4 paper execution) v0.4.0

The long-running process that places the trades. M2 proved the data plane (broker logins, DXLink stream, 1-minute bars,
5-minute chain snapshots with an OI dealer-gamma proxy, halts, VIX term, SQLite + Supabase mirror, Telegram heartbeat /
EOD report); M3 added the **rules engine** — six gates, window scheduler, theta clock, Tier 1 rails, fractional Kelly with
shrinkage, and a **shadow ledger marked at bid (mid − half spread) from DXLink**, replayable byte for byte from the
session's recording; **M4 adds paper execution in the tastytrade sandbox** — every gate-fired entry is proposed on Telegram
(Approve / Skip, 3-minute timeout = Skip), filled through a limit-at-mid retry ladder, reconciled against the broker every
30 s, and killable within 10 s (`/halt` or a `state/HALT` file). Production brokerage stays refused by config until M5.

Source of truth: Foundry `projects/seeking-alpha-agent/src/daemon/` (this folder). Deployed copy:
`Desktop/Seeking Alpha Agent /agent/daemon/` on Ryan's Mac (M2–M4), `/opt/saa/agent/daemon/` on the VPS (M5+).

## Run it

```bash
cd "$HOME/Desktop/Seeking Alpha Agent /agent/daemon"
./run.sh check        # .env readiness — key names only, never values
./run.sh smoke        # ~30 s: logins, DXLink token + one quote + feed lag, chain, VIX, halts, SQLite, Supabase write,
                      #        engine (rules version, today's checklists, posterior, Kelly table), Telegram
./run.sh session      # today's session; exits after the 16:20 report (start any time before 16:25 ET)
./run.sh kelly-table  # the plan §0.3 table (6% / 16% / 24%) and the sizing grid at the current account size
./run.sh rules        # the Tier 2 parameters the engine would run with (saa.rules latest, Tier 1 keys ignored)
./run.sh replay state/recordings/<run>.jsonl --engine [--out ledger.json]   # deterministic replay → ledger + sha256
./run.sh paper-roundtrip [--symbol SPY] [--n 3] [--allow-delayed]   # M4 evidence: sandbox round trip(s) through the ladder, reconciled
./run.sh halt-test [--allow-delayed]      # M4 evidence: open 1 contract in the sandbox, kill switch → flat, seconds measured (≤ 10)
./run.sh approval-test [--timeout 180]    # M4 evidence: a real Telegram proposal with buttons; no answer = logged as Skip
./run.sh halt | ./run.sh resume           # engage / clear the kill switch by hand (file flag + saa.settings.halt)
./run.sh paper-status                     # today's paper book from SQLite
```

`run.sh` needs Python 3.11+ (`brew install python@3.12` if missing); the first run creates `.venv/` and installs
`requirements.txt`. Everything it writes lives under `state/`: `state/saa.sqlite` (hot state), `state/recordings/<run>.jsonl`
(market events + engine records, for replay), `state/logs/`.

Scheduled: `deploy/com.saa.daemon.plist` (launchd, 09:10 ET weekdays — the Mac must be awake **and** the agent needs
file access, see the header: macOS refuses a LaunchAgent in `~/Desktop` until `/bin/bash` has Full Disk Access or the
daemon lives outside Desktop) or `deploy/saa-daemon.service` (systemd, `forever` mode) — instructions in each file's header.

## `.env` keys (in the Seeking Alpha Agent folder, one level above `agent/`)

| Key | Used for | Required |
|---|---|---|
| `TT_PROD_CLIENT_ID` / `TT_PROD_CLIENT_SECRET` / `TT_PROD_REFRESH_TOKEN` | **market data** (DXLink, chains) — read-only | yes |
| `TT_SANDBOX_CLIENT_ID` / `TT_SANDBOX_CLIENT_SECRET` / `TT_SANDBOX_REFRESH_TOKEN` | the paper account (M4 orders); read only until then | yes |
| `SUPABASE_URL` | `https://zspbkcheounkwnpjkgrv.supabase.co` | yes (mirror, rules, checklists) |
| `SUPABASE_SERVICE_ROLE_KEY` (or `SUPABASE_SECRET_KEY`) | reads rules/checklists/ledger and writes to `saa.*` through the `public.saa_*` RPCs — a new `sb_secret_…` key (sent on `apikey` only) or the legacy `service_role` JWT | yes |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | **fallback** delivery only; the primary path is `saa.outbox` | optional |
| `SAA_DATA_ENV` | `prod` (default) or `sandbox` — the sandbox has no market data (D11) | optional |
| `SAA_INDEX_SYMBOLS` | overrides `saa.settings.index_symbols` (default SPY,QQQ,IWM) | optional |
| `SAA_EXTRA_SYMBOLS`, `SAA_MAX_SINGLE_NAMES` (25), `SAA_STRIKE_WINDOW_PCT_INDEX` (3), `SAA_STRIKE_WINDOW_PCT_SINGLE` (8), `SAA_MAX_STRIKES_PER_SIDE` (20), `SAA_EXPIRATIONS_INDEX` (2), `SAA_EXPIRATIONS_SINGLE` (1), `SAA_SNAPSHOT_MINUTES` (5), `SAA_RECORD_EVENTS` (true), `SAA_MIRROR` (true), `SAA_LOG_LEVEL` (INFO), `SAA_STATE_DIR` | tuning | optional |

`SAA_BROKER_ENV` is fixed to `sandbox`; setting anything else is a configuration error until M5. `SAA_EXECUTION=false`
turns the M4 paper executor off (engine + shadow ledger only). Secrets are wrapped so they cannot appear in logs or
reports; any known secret value is redacted from every log line as a second guard. The engine has **no** `.env` knobs on
purpose: Tier 1 is code, Tier 2 is the `saa.rules` table. `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are **required for
M4's approvals** (inline buttons and `/halt` need the Bot API directly; the outbox cannot carry buttons).

## What a session does (ET)

| When | What |
|---|---|
| start → 09:20 | wait (a late start joins in progress) |
| 09:20 | log in (production data + sandbox account), read `saa.settings.index_symbols`, `saa_active_symbols()` (brief watch list, triggers, open shadows) capped at 25 names, spot prices, nested chains → strike windows (indices ±3 %, 2 expirations; names ±8 %, 1 expiration; ≤20 strikes per side), subscribe DXLink; **load the engine**: `saa_rules_latest()`, today's `saa_checklists_today()`, `account_size` / `kelly_k` settings, the last 60 closed gate-fired trades (`saa_engine_ledger`), the cooling-off flag |
| 09:25 | Telegram heartbeat (via `saa.outbox`) with the Engine line + the pre-open baseline snapshot |
| 09:25–16:20, 2 s into every minute | **engine tick**: mark digest per underlying (planned options within ±1.5 % of spot) → recorded → manage open shadows (trail / mechanism / time stop) → consider entries (six gates, fast lanes) → persist ledger + journal → Telegram alert on a gate-fired open/close → **the executor** gets the tick's open / bank / close events (M4) |
| while the session runs | **paper executor (M4)**: a gate-fired open → `Proposed ▸ …` on Telegram with Approve / Skip buttons (3-minute timeout = Skip) → on Approve a limit-at-mid order in the sandbox, re-priced toward the ask every 5 s (3 rungs), cancelled if still unfilled → the engine's close / bank event sells the same way (escalating to market if an exit will not fill) → every order, fill, proposal and reconciliation mirrored to `saa.paper_*`; reconciliation vs the broker every 30 s; `/halt` flattens everything (bid → bid − step → market) and blocks entries until `/resume`; the Telegram bot is long-polled from the daemon (the edge function stands down while the daemon is alive) |
| 09:30–16:00 | bars flushed to SQLite every 5 s; chain snapshot per underlying every 5 min (compact strike arrays + summary + gamma proxy); Cboe VIX term every 5 min; Nasdaq halts RSS every 60 s; watch list re-read every 5 min; Supabase mirror every 10 s; machine heartbeat (`saa_daemon_run`) every 60 s |
| 16:20 | final engine tick (anything still open is closed at the bid) → the executor closes any paper position still open and expires pending proposals → one more reconciliation → Telegram EOD report with the engine's and the paper book's day; `saa.run_log` row `daemon:session` with bar counts, snapshot counts, engine counts, execution counts, errors |
| 16:25 | exit |

A websocket drop raises inside the feed task; the supervisor restarts it with backoff and dxfeed replays the day's
candles, which the bar book upserts idempotently. Every task is supervised: the acceptance criterion is zero *unhandled*
exceptions, and the EOD report lists the caught ones.

## The engine (M3) — `saa_daemon/engine/`

**Live evaluation is gated on `feed_lag.mode == realtime`.** The feed-lag monitor measures the median exchange→receipt
delay of underlying trades (~1 s = real-time; ~900 s = the 15-minute delayed feed found on 2026-09-28). While the feed is
not real-time the engine runs **observe-only**: no shadow trades, one journal row per symbol per window saying so, the
heartbeat and EOD say "observe-only". Everything else below can be exercised offline (tests, `replay --engine --eval`).

| Piece | File | What it does |
|---|---|---|
| Tier 1 rails (code) | `tier1.py`, `rails.py` | k ∈ [0.5, 1.0]; shrinkage n0 = 30; one-contract floor only while account < $2k and the contract ≤ $150; never above full Kelly; order caps (≤ 2 contracts per $1k, ≤ 50 % of account); edge-loss halt (rolling-30 expectancy < 0 → floor, rolling-60 < 0 → stop); daily −3R stop; 3-loss lockout; cooling-off after a ≥ +5R win or ≥ +8R day (next session half size); no fresh entries 11:30–13:30; release blackout T−15…T; every entry carries a time stop; spread filter 5 % flag / 10 % skip. A `saa.rules` row carrying these keys is ignored and logged. |
| Tier 2 rules (data) | `rules.py` | `saa.rules` latest version (v2 from the M3 build): bank fraction, trail, 0.5σ OTM strike, windows, fast lanes, gate thresholds, mechanism exits, max concurrent positions, alert cap. Missing keys → defaults; validated. |
| Windows + theta clock | `windows.py` | open 09:30–10:00 (entries to 09:58; 09:35 on a CPI/NFP day), mid (named trigger only), dead zone, afternoon (event windows only), last hour 15:00–15:55 (negative gamma only); per in-session release: blackout T−15…T, entry T+5…T+15, stop T+55; FOMC presser window T+30. Theta clock √(time left / 6.5 h): 0.920 at 10:30 … 0.392 at 15:00. |
| Native triggers | `triggers.py` | ORB (first 5 bars, break on ≥ 1.5× volume, until 10:30), VWAP reclaim/loss (≥ 15 bars, until 15:30), first-hour continuation (new session high/low on ≥ 1.2× volume, 10:00–11:30), RVOL (inert until a 20-day per-minute volume baseline exists), realized vol, failed new extreme, volume taper. |
| Six gates | `gates.py` | catalyst (brief checklist, or a scheduled release for an index), readable (gamma proxy coverage ≥ 50 % + VIX for indices; chain coverage for names), favorable (index: proxy negative; names: brief), wedge (RV 60 m > ATM IV when measurable, else brief), direction (brief; two-sided → one contract per leg), trigger (native signal in the thesis direction, or the first post-release bar). **STAND DOWN is the default**; stand-downs are journaled. |
| Kelly | `kelly.py` | posterior (p, W) = prior-mixed (n0 = 30, prior 30 %/5R) and edge-shrunk toward breakeven (n = 0 → ε only → the floor); `size = k·f*`, capped at full Kelly and the order caps, floored per Tier 1. `kelly-table` prints the plan §0.3 table. |
| Positions | `positions.py` | entry at ask, marked at **bid**; partial bank at +7.5 % of account (sell enough to recoup the premium, or the whole position trails when one contract); trail = HWM − max(30 % of gain, 1-minute option ATR), 20 % once ≥ +3R (latched); mechanism exits override the trail (VWAP loss for single names, failed new extreme, volume taper); time stop at the window edge, pulled in by a release blackout and by close − 5 min. R = contracts × ask × 100; `r_result` = (proceeds − cost) / cost. |
| Engine | `engine.py` | the minute tick, fixed order (positions first, then entries, symbols sorted), dedupe (one gate position per symbol per window per day; one fast-lane shadow per symbol/lane/direction per day), journal, alerts (gate-fired only, ≤ 20/day), canonical JSON output. |
| Replay | `replay.py` | reads a recording (`Meta`, `Plan`, market events, `OptMarks`, `Tick`) and runs the same ticks → the same bytes. A pre-M3 recording (events only) gets synthesized ticks and the recording's own feed mode. |

Fast-lane hypotheses (ORB / VWAP / continuation / RVOL) open **one contract, zero capital**, never touch the rails, and
exist to measure the lanes (promotion needs ≥ 60 shadow trades with expectancy > 0 after costs — `saa.rules.promotion_gate`).

## Paper execution (M4) — `saa_daemon/execution/`

**The order path sits behind the same real-time gate as the engine (D19)**: an entry is refused with `feed_not_realtime`
unless `feed_lag.mode == realtime`, even if an engine event somehow arrived (belt and braces — the engine itself fires
nothing on a delayed feed). Fast-lane hypotheses never trade.

| Piece | File | What it does |
|---|---|---|
| Broker boundary | `broker.py`, `tastytrade_broker.py` | `place / replace / cancel / get_order / live_orders / positions / balances` on OCC symbols. `FakeBroker` (tests, dry runs: marketable / at-limit / never / partial / reject modes); `TastytradeBroker` on the SDK's `LimitOrder` / `MarketOrder` with one equity-option leg — `buy_to_open` and `sell_to_close` only, sandbox only (production is M5). |
| Symbols | `symbols.py` | streamer ↔ OCC (`.SPY260928C654` ↔ `SPY   260928C00654000`), penny-pilot ticks ($0.01 < $3, $0.05 above), rounding toward the far side. |
| Order manager | `orders.py` | one ticket = the limit-at-mid **retry ladder**: mid (rounded toward the far side) → re-priced every 5 s a step closer → the far side on the last rung → 5 s grace → cancel. Partial fills are never replaced, only cancelled at the end (→ `partial`). **Flatten ladder** for the kill switch: bid at once, bid − max(1 tick, 5 %) at 3 s, market at 6 s. Every placement / replace / fill / cancel is logged (`paper_orders`). |
| Approvals | `approvals.py`, `telegram_bot.py` | `Proposed ▸ SPY long call .SPY… ×1 @ ask 1.16 (mid 1.14) · R $116 (floor) · open · 6/6 gates · p=0.32 · stop 10:00` with ✅ Approve / ⏭ Skip; a tap resolves it (the message is edited with who / when / latency), **no answer in 3:00 = Skip (status `timeout`)**, closed-by-the-engine-first = `expired`. Commands: `/halt`, `/resume`, `/status`, `/positions`, `/help`, `/id`. Only the owner's chat is honoured. Offsets are shared with the `telegram-send` edge function through `saa.settings.telegram_update_offset`; the function skips `getUpdates` while `daemon_last_seen` is < 3 min old. |
| Executor | `executor.py` | engine open → (kill switch? feed real-time? Tier 1 caps at the live ask: ≤ 2 contracts per $1k, ≤ 50 % of the account?) → proposal → ladder → `open`; close / bank → exit ladder (escalates to flatten); `halt()` expires proposals, cancels working orders, flattens every open position **and any sandbox position the book does not know**, reports the seconds to flat; `reconcile()` every 30 s compares broker positions + live orders with the book (`saa.reconciliations`, mismatches alerted once). Realized R = (proceeds − cost − fees) ÷ cost; slippage vs the engine's ask / bid is recorded per trade. |
| Kill switch | `killswitch.py` | the `state/HALT` file (survives restarts) + `saa.settings.halt` (the M1 `/halt` path) kept in step; `./run.sh halt` / `/halt` engage, `./run.sh resume` / `/resume` clear. A halted session starts halted and says so in the heartbeat. |
| Self-tests | `paper.py`, `paper_cli.py` | `paper-roundtrip`, `halt-test`, `approval-test` — the M4 acceptance evidence, run from the Mac against the real sandbox; the flows are tested offline. |

Mode is `approval` and cannot be set to `auto` until M6 (`ALLOW_AUTO_MODE` in `executor.py`). Exits never need approval.

## Where the data lands

| SQLite (`state/saa.sqlite`) | Supabase (`saa.*`) via RPC |
|---|---|
| `bars_1m` | `saa.bars_1m` (`saa_bars_upsert`) |
| `chain_snapshots` | `saa.chain_snapshots` (`saa_chain_snapshot`) |
| `vix_term` | `saa.market_snapshots` kind `vix_term` (`saa_snapshot`) |
| `halts` | `saa.halts` (`saa_halts_upsert`) |
| `engine_trades` (shadow ledger, keyed by `engine_key`) | `saa.shadow_trades` rows with `source = 'engine'`, `model_version = 'dxlink-marks-v1'` (`saa_engine_shadow_upsert`) |
| `engine_decisions` (journal) | `saa.engine_decisions` (`saa_engine_decisions_insert`) |
| `paper_trades`, `paper_orders`, `approvals`, `reconciliations` (M4) | `saa.paper_trades` / `saa.paper_orders` / `saa.approvals` / `saa.reconciliations` (`saa_paper_trades_upsert`, `saa_paper_orders_upsert`, `saa_approvals_upsert`, `saa_reconciliations_insert`) |
| `runs`, `events_log`, `kv` (cooling-off) | `saa.daemon_runs` (`saa_daemon_run`), `saa.run_log` (`saa_log_run`), `saa.settings.engine_cooling_off_after` |
| `mirror_queue` | durable retry queue for outbox / run_log / settings calls while Supabase is unreachable |

Reads at session start: `saa_rules_latest`, `saa_checklists_today`, `saa_engine_ledger`, `saa_get_setting`
(`account_size`, `kelly_k`, `engine_cooling_off_after`, `halt`, `telegram_update_offset`), `saa_active_symbols`, `saa_calendar_day`.
The dashboard (`src/dashboard/`) reads one RPC, `saa_dashboard(p_days)`, server-side.

Chain snapshot strike rows are arrays in this order:
`[strike, call_bid, call_ask, call_iv, call_delta, call_gamma, call_oi, call_volume, put_bid, put_ask, put_iv, put_delta, put_gamma, put_oi, put_volume]`.
Mark digest entries (`OptMarks.marks[symbol]`): `[bid, ask, iv, delta, gamma, theta, oi, volume, quote_ms, recv_ms]`.

Dealer-gamma proxy per snapshot: `net_gex / call_gex / put_gex` (dollar gamma per 1 % move = gamma × OI × 100 × spot² × 0.01,
calls +, puts −), `flip`, `call_wall`, `put_wall`, `regime`, `coverage`. The engine reads `regime` and `coverage` for gates 2–3
(index windows, last hour); the ledger measures whether the sign is right (synthesis §8).

## Tests

From `projects/seeking-alpha-agent`: `python3 -m pytest src/tests -q` (135 tests; needs `pip install -r src/daemon/requirements-dev.txt`
— pytest, pytest-asyncio, hypothesis). M4: `test_execution_orders.py` (symbols, ticks, the ladder on the FakeBroker: fills at mid,
steps to the ask, re-reads the quote, gives up and cancels, partial, rejected, transport error, outside cancel, the flatten ladder
reaching market inside 10 s), `test_execution_executor.py` (approve → entry → engine close → exit with realized R and slippage;
skip; **3-minute timeout logged as Skip**; expired; no channel fails closed; delayed feed blocks the order path; fast lanes never
trade; Tier 1 caps refused at the order and re-checked at the live ask; auto mode refused until M6; exit escalation; bank;
**halt flat within 10 s incl. a stranger position**, file flag survives a restart; reconciliation matches and flags strangers
once; end of day), `test_execution_bridge.py` (SDK 13.x order/position/balance shapes; Telegram send/edit/poll/409),
`test_execution_session_sim.py` (three whole days through the daemon loop with a FakeBroker on the sandbox seat and a scripted
Telegram: a round trip proposed → approved 9 s later → filled → closed → reconciled → mirrored → in the EOD text; a `/halt` day;
a delayed-feed day placing nothing), `test_paper_cli.py` (the self-test flows). Earlier highlights: `test_engine_kelly.py` (plan §0.3 table, shrinkage, sizing rails as
Hypothesis properties), `test_engine_rails.py` (every Tier 1 rail attacked through the engine), `test_engine_core.py`
(rules, windows, theta clock, triggers, gates, exits), `test_engine_replay.py` (5 synthetic recordings replayed twice →
identical sha256; a pre-M3 recording; the real 2026-09-28 recording when its fixture is present), `test_engine_session_sim.py`
(a whole day through the daemon loop: a sized SPY shadow fires and closes, journal + ledger mirrored, **replaying the
session's own recording reproduces the live decisions/ledger/state byte for byte**; a delayed-feed day is observe-only),
`test_daemon_session_sim.py` (the M2 data-plane day).
