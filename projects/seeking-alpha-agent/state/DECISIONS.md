# Decisions — Seeking Alpha Agent — Autonomous Intraday Options Agent

Lightweight decision log. Newest on top. Record anything a future session (or future
Ryan) would otherwise re-litigate.

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
