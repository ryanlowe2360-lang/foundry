# Decisions — Seeking Alpha Agent — Autonomous Intraday Options Agent

Lightweight decision log. Newest on top. Record anything a future session (or future
Ryan) would otherwise re-litigate.

## D15 (2026-09-27) — Spot prices come from DXLink; tastytrade REST market data is a fallback only

- **Context:** the second real smoke run (18:19 ET) showed `/market-data/by-type` and `/market-data/equity/<sym>` answer
  **403 Forbidden** on Ryan's production OAuth app (not entitled to REST market data), and a retry produced a **429** from
  nginx; DXLink quote tokens and streaming work fine (SPY 771.89, QQQ 745.30, IWM 282.23 via the streamer).
- **Chose:** `Brokerage.spot_prices` probes DXLink first (6-second quote window), then REST by-type, then REST per symbol;
  after any 403 the REST steps are skipped for the rest of the process. Option Quote/Greeks DXLink aggregation set to 2 s
  (snapshots are 5-minute; halves the event rate at the open).
- **Revisit if:** tastytrade grants the app REST market-data access (then REST could seed spots faster on cold start) or
  the DXLink probe proves slow at 09:20 (then start the underlying feed first and plan chains from its quotes).

## D14 (2026-09-27) — `saa.v_daily_records` shows only dates ≤ today (migration 0006)

- **Context:** the hand-maintained econ calendar (D13 scope) puts 26 future rows into `saa.calendar_days`; the
  market-data function already pre-fetches the next trading day. The Friday review counts the M1 record streak as
  "consecutive `complete` days from the most recent" over `select * from saa.v_daily_records limit 20`, which future
  rows would read as incomplete days.
- **Chose:** filter the view to `trade_date <= saa.et(now())::date` and weekdays. Same columns; no prompt changes.
- **Revisit if:** a report needs the forward calendar — read `saa.calendar_days` directly.

## D13 (2026-09-27) — Daemon writes to Supabase through the `public.saa_*` RPC surface with the service-role key from `.env`

- **Context:** the daemon runs on Ryan's Mac (M2–M4) and must mirror into `saa.*` and enqueue Telegram messages through
  `saa.outbox`. PostgREST does not expose the `saa` schema; the M1 pattern is RPCs in `public` locked to `service_role`.
- **Options:** (a) service-role key in `.env`, RPC only (this); (b) a dedicated edge function with a shared secret;
  (c) direct Postgres connection string in `.env`.
- **Chose:** (a). Seven new RPCs in migration 0005 (`saa_bars_upsert`, `saa_chain_snapshot`, `saa_halts_upsert`,
  `saa_daemon_run`, `saa_calendar_day`, `saa_econ_upsert`, `saa_daemon_status`), all `security definer`, `search_path`
  pinned, execute granted to `service_role` only; new tables `saa.bars_1m`, `saa.chain_snapshots`, `saa.halts`,
  `saa.daemon_runs` (RLS on, no policies). Retention added to the daily housekeeping cron (bars 120 d, snapshots 45 d,
  halts/runs 180 d). Ryan pastes the key himself; it never passes through chat. Every mirror write is an idempotent
  upsert so a network outage is a delay, never a loss (SQLite `mirrored` flags + a durable RPC queue).
- **Revisit if:** the VPS (M5) should get a narrower credential — then a dedicated Postgres role or an ingest edge function.

## D12 (2026-09-27) — Daemon universe and snapshot shape

- **Context:** the spec names SPY/QQQ/XSP + the day's watch list; `saa.settings.index_symbols` (set at intake and used by
  the M1 poller and brief) says SPY,QQQ,IWM. Chain snapshots for many names every 5 minutes could outgrow the free tier.
- **Chose:** the daemon reads `saa.settings.index_symbols` as the single source of truth (`SAA_INDEX_SYMBOLS` in `.env`
  overrides; XSP is a one-line setting change, the code already treats it as a Cboe index for REST spots). Single names
  come from `saa_active_symbols()` (brief watch list, today's triggers, open shadow trades, non-stand-down checklists),
  re-read every 5 minutes, capped at 25 (`SAA_MAX_SINGLE_NAMES`). Strike windows: indices ±3 %, 2 nearest expirations
  (0DTE + next); names ±8 %, 1 expiration; ≤20 strikes per side; option symbols subscribed in chunks of 150. Snapshot rows
  are compact 15-column arrays (≈3 KB per index snapshot) with a summary (ATM IV, OI/volume totals, put/call ratios,
  coverage) and the OI-based dealer-gamma proxy (dollar gamma per 1 % move, calls +, puts −, flip level, call/put walls,
  regime, coverage) — recorded for the ledger to measure, not used as a rule (synthesis §8). Recording keeps
  underlying-level events only (option state is captured by the snapshots).
- **Revisit if:** M3 needs full option-quote replay (record option events too, ~300 MB/day) or the brief wants more names.

## D11 (2026-09-27) — Market data from production OAuth; the sandbox is the account only; Telegram via outbox with a direct fallback

- **Context:** the tastytrade sandbox has no market data (recorded 2026-09-26) and the SDK's streamer requires a
  production session. The spec's "OAuth (sandbox first)" is honoured for the *account*; quotes have to come from
  production.
- **Chose:** two sessions in the daemon: `data` = production (`TT_PROD_*`, read-only: DXLink quote token, nested option
  chains, REST market data) and `broker` = sandbox (`TT_SANDBOX_*`, read in M2, orders in M4). `SAA_BROKER_ENV` is
  hard-fixed to `sandbox` until M5 and there is no order code in the package. Heartbeat / EOD report go through
  `saa_enqueue` → `saa.outbox` → the existing insert trigger → `telegram-send` (spec path); if Supabase is unreachable
  the daemon sends the same text through the Bot API directly from the Mac (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
  from `.env`) and logs that it did; if both fail the enqueue is queued durably and retried.
- **Revisit if:** tastytrade opens sandbox market data (then `SAA_DATA_ENV=sandbox` is one line) or the outbox path
  proves reliable enough that the fallback is noise.

## D10 (2026-09-27) — Trigger feed for M1–M2 stays TradingView alerts; universe = SA Quant watchlist + SPY/QQQ/IWM

- **Context:** Ryan raised the weaknesses of per-symbol TradingView alerts (setup chore, alerts bound to the
  script version, universe doesn't follow the brief). Claude offered an Alpaca free-bars addendum with
  native triggers in the database.
- **Chose (Ryan):** keep TradingView alerts as the fast-lane feed for now, on the Seeking Alpha Quant
  watchlist (`saa.symbols` active tiers A/B, 133 names) plus SPY/QQQ/IWM; Ryan sets the alerts himself.
  The Alpaca addendum is shelved, not rejected — the M2 daemon computes the same triggers natively from the
  broker feed, which retires the alert chore on its own schedule.
- **Consequence:** ~136 open-ended alerts (Premium allowance permitting; tier A + indices first if the plan
  caps lower). Every alert must be recreated whenever the Pine script changes — so the script is frozen at
  v1 until M2 unless a bug forces a change.
- **Revisit if:** alert maintenance becomes the bottleneck before M2, or the plan's alert cap binds.

## D9 (2026-09-27) — Goal 2 (Trading) in the Goals ledger: KILL — final (supersedes D8 and D3)

- **Context:** Ryan asked for a plain explanation of Goal 2 and whether it was part of this build. It is not:
  it is a separate personal-ledger goal (≤5 hr/wk cap, journal streak, quarterly P&L) whose only overlap
  with this project was the friction rule on trading-tool builds.
- **Chose:** KILL (Ryan, 2026-09-27 11:45 ET). Logged in `Desktop/Goals/02-Trading/GOAL.md` (status KILLED,
  log entry) and `GOALS.md` (row removed from active goals; "Killed" section added). Trading is scored
  only by this project's own gates from here on; nothing in the build changes.
- **Revisit if:** Ryan wants a personal time budget for trading again — that would be a new ledger goal,
  not a reopening of this one.

## D8 (2026-09-26) — Goal 2 (Trading) ruling revised to REVISE, keep (supersedes D3)

- **Context:** After the intake (D3 = KILL), Ryan ruled REVISE in a later setup session the same day, per
  the Project build log: zero-line weekly indicator (realized vs required growth per trade), ≤5 hrs/week
  cap unchanged, M1 counts as the journal from day one.
- **Chose:** REVISE stands; D3 is superseded. The 10-day complete-records streak is both the ledger's
  journal streak and the M1 → M2 engineering gate.
- **Revisit if:** Ryan reopens the ruling in a goals session.

## D7 (2026-09-26) — The Foundry auto-build stays off this project via a recorded blocker

- **Context:** Ryan's "Foundry daily auto-build" scheduled task picks the stalest in-flight project with no
  blockers/open questions and builds a milestone unattended. M2+ needs tastytrade sandbox OAuth; M1's
  remaining acceptance needs Ryan's secrets. An unattended run touching live cron/edge functions is
  not wanted.
- **Chose:** keep a truthful blocker in STATE.json until Ryan clears the setup steps; build sessions
  are interactive ("M2 build" chat in the Project).
- **Revisit if:** Ryan wants nightly unattended builds here — then clear the blocker after M2's keys exist.

## D6 (2026-09-26) — Scheduled Claude tasks are the M1 judgment layer; each run guards against duplicates

- **Context:** The 7:40 brief, 4:20 tally and Friday review run as Anthropic scheduled tasks (fresh
  sessions, Supabase connector attached, automatic approval). A manual `fire_trigger` with appended text
  is delivered as a second turn after the prompt's first turn completes.
- **Chose:** prompts are complete standalone instructions (`src/prompts/`), write only via `public.saa_*`
  RPC + plain SQL through the connector, never HTTP; the brief refuses to run twice for a date and
  deletes dry-run checklists before writing real ones; every run logs to `saa.run_log`.
- **Revisit if:** the connector is missing in a scheduled run (fallback: a pg_cron-driven edge function
  calling the Claude API directly) or the brief needs Ryan's browser (Seeking Alpha reads) — then that
  part moves to an interactive morning session.

## D5 (2026-09-26) — Shadow trades in M1 are scored on the underlying's 1-min quote path with a modeled 0DTE option price

- **Context:** M1 has no options data (DXLink arrives in M2) and Finnhub's free tier has no
  intraday candles for US stocks, only quotes.
- **Options:** (a) skip scoring until M2; (b) unofficial Yahoo chart API for 1-min candles;
  (c) poll Finnhub `/quote` every minute for symbols with open shadow trades and model the
  option with Black-Scholes on that path.
- **Chose:** (c). Officially supported feed, ~1 call/symbol/minute is well inside the free
  60/min limit, and the model is explicit and testable (`src/tests/test_shadow_model.py`).
  σ_d comes from Cboe VIX1D for index symbols and a per-symbol daily-vol setting for single
  names (default 2.5%, Tier 2 learnable). Entry premium modeled at mid + half of a modeled
  spread (max(5% of premium, $0.02)); exit at mid − half spread.
- **Revisit if:** M2 DXLink marks show the modeled R differs systematically from real marks
  by more than the modeled spread — then M1 shadow rows get a `model_version` discount.

## D4 (2026-09-26) — All outbound calls run inside Supabase edge functions driven by pg_cron/pg_net; scheduled Claude sessions use only the Supabase connector

- **Context:** The cloud build container reaches finnhub.io and api.github.com but the proxy
  refuses api.telegram.org, cdn.cboe.com, *.supabase.co and supabase.com (CONNECT 403).
  Scheduled Claude sessions run in the same environment.
- **Options:** (a) scheduled sessions curl the edge functions (blocked); (b) Ryan's Mac runs
  a poller (defeats "no Mac uptime" for M1); (c) pg_cron + pg_net call the edge functions
  from inside Postgres, an `outbox` insert trigger delivers Telegram immediately, and the
  Claude sessions read/write `saa.*` through the Supabase MCP connector only.
- **Chose:** (c). Zero dependence on the container's network; every side effect is a row.
- **Revisit if:** the connector is unavailable in scheduled runs (M1 acceptance #5 tests this)
  — fallback is a Supabase edge function that calls the Claude API directly on a pg_cron
  schedule.

## D3 (2026-09-26) — Goal 2 (Trading) in the Goals ledger: KILL

- **Context:** Plan §0.4 / §10 item 6 left the 8/19 RECOMMIT/REVISE/KILL ruling pending;
  Goal 2's friction rule ("no new Trading.tools builds until a 12-week journal streak") would
  otherwise block this build.
- **Options:** RECOMMIT / REVISE (add a realized-vs-required indicator) / KILL / defer.
- **Chose:** KILL (Ryan, intake interview). This spec and the Foundry ledger are the record
  for the trading effort; the 10-day complete-records streak survives as the M1 → M2 gate.
- **Revisit if:** Ryan reopens the Goals ledger for trading — then REVISE is the consistent
  re-entry (weekly indicator: realized vs required growth per trade).

## D2 (2026-09-26) — Supabase home is project "Quant edge" (`zspbkcheounkwnpjkgrv`), schema `saa`

- **Context:** The org has two active projects (free-tier cap); a new project needs Pro.
- **Options:** new project (Pro) / schema in "Quant edge" / schema in "agent-hub".
- **Chose:** schema `saa` in "Quant edge" (Ryan). Isolation by schema; `public.*` untouched;
  functions and cron jobs prefixed `saa_`/`saa-`.
- **Revisit if:** the earlier Quant edge system is retired (then the schema can move) or the
  org upgrades to Pro (then a dedicated project is cleaner for secrets and logs).

## D1 (2026-09-26) — The Foundry is the build engine; code lives in `projects/seeking-alpha-agent/src/`

- **Context:** Plan §11 named the Foundry as the intended engine but a memory note said it was
  dead; the repo (`ryanlowe2360-lang/foundry`) clones, validates and passes its 13 tests.
- **Options:** Foundry vs. a git repo in the Seeking Alpha Agent Desktop folder.
- **Chose:** Foundry. State discipline is enforced by its validator; the Project docs
  (`claude/build-log.md`) mirror the status for chat sessions. Ryan-facing files (Pine script,
  setup guide) are also copied into `Desktop/Seeking Alpha Agent/agent/` for convenience.
- **Revisit if:** `src/` outgrows the nest (graduation rule) — then a dedicated repo
  `seeking-alpha-agent` with the Foundry as control plane.
