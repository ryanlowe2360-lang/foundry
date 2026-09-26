# Seeking Alpha Agent — Autonomous Intraday Options Agent

**Slug:** `seeking-alpha-agent` · **Created:** 2026-09-26
**Lock status:** LOCKED: 2026-09-26

Source of record for the *why* and the numbers: `spec/original/build-plan-v0.4-seeking-alpha-agent.md`
(Ryan's plan, v0.4). The research the plan rests on is `claude/corpus-synthesis.md` in the
claude.ai Project "Seeking Alpha Agent" (a full read of the 46-file corpus). This spec turns the
plan into milestones a stranger can build and verify one session at a time.

## One-liner

A rules engine that places intraday long-option trades (calls/puts) from a $1,000 tastytrade
account on Ryan's behalf, with Claude doing the slow judgment jobs (pre-market brief, regime read,
journal, Friday review) and every rule change gated on measured hit rate and payoff.

## Problem & user

Ryan (accounts-payable analyst by day, self-taught developer) wants an agent that trades
autonomously pre-market and through the session while he works. Objective as stated
2026-09-25: grow $1,000 → $5,000,000 in 12 months, aggressively, front-loading returns while the
account is small and accepting that the daily percentage falls as the account grows. Success in
one sentence: **the agent runs every trading day without Ryan, sizes from measured p and W, and
the ledger shows realized-vs-required growth per trade toward $5M by 2027-09-25.**

## Goals (ranked)

1. Measure the edge before betting on it: a journal + shadow ledger that produces the first
   hit-rate (p) and payoff (W) estimates within two weeks (M1).
2. A deterministic rules engine (six gates, Kelly sizing, Tier 1 rails) that trades paper, then
   live with approval, then fully auto — each rung behind a numeric gate.
3. A rulebook that rewrites itself only on evidence (Tier 2 changes: ≥30 relevant trades, one per
   week, logged, reversible).
4. Ryan's operating burden ≤ 1 hr/week: read the brief (optional), watch Telegram, tap
   Approve/Skip (M3 only), one Friday review.

## Non-goals — the cut list

Explicitly out of scope for v1 (revisit via Improve addenda only):

- **Short options, spreads as the default structure, stock positions** — the instrument is long
  premium; a vertical only appears as a Stage-2 cascade structure later (corpus §6).
- **A daily percentage quota** — the daily objective is a window count; 3.44%/day is shown as
  required-vs-realized only, never as a target the engine chases.
- **Backtesting on paid option data (ThetaData etc.)** — the corpus says fit thresholds to the
  trade log; forward-test with the shadow ledger instead.
- **TrendSpider, Unusual Whales, Benzinga squawk, any paid data** — $0 data budget until the
  ledger justifies a purchase.
- **Automated Seeking Alpha scraping** — ToS; SA is read by Claude through Ryan's browser only
  when he is present; unattended briefs skip it.
- **Alpaca as the primary broker** — its 3:15/3:30 PM expiring-option cutoffs kill the final-hour
  plays; Alpaca is a paper harness only.
- **A trading loop on Vercel/serverless or inside a chat session** — the daemon runs on a VPS
  (M2+); M1 is serverless *because it places no orders*.
- **A dashboard before M2** — Telegram messages are the M1 UI.
- **The swing-momentum sleeve** (Seeking Alpha quant ratings) — separate project if ever wanted.

## Milestones

Each milestone is completable in roughly one session and has testable acceptance criteria.
Build order is the numbered order unless a decision log entry says otherwise. Plan-milestone
numbers (the plan's M0–M4) are given in parentheses; the plan's M2 is split into three sessions.

### M1 — Proving Ground: journal-only pipeline (plan M1)
- **Builds:** Supabase schema `saa` in project "Quant edge" (`zspbkcheounkwnpjkgrv`): `settings`,
  `calendar_days`, `market_snapshots`, `price_ticks`, `triggers`, `shadow_trades`, `briefs`,
  `checklists`, `calibration`, `reviews`, `outbox`, `run_log`, all RLS-on with no anon policies.
  Edge functions: `tv-webhook` (TradingView alerts → `triggers` + opens shadow trades; auth by
  body secret + TradingView source-IP check), `telegram-send` (flushes `outbox` to Ryan's DM;
  captures the chat id from `/start`), `market-data` (Finnhub earnings/IPO calendar, Cboe VIX
  term structure, index quotes → `calendar_days`/`market_snapshots`; 1-min quote polling for
  active symbols → `price_ticks`), `shadow-scorer` (closes shadow trades on the underlying's
  tick path with a modeled 0DTE option price; trail + time stop applied). pg_cron drives the
  functions; an insert trigger on `outbox` delivers Telegram immediately. Three Anthropic
  scheduled tasks: 7:40 AM ET brief (Stage 0–4 funnel → six gates → 15-item arming score →
  checklist with probability → Telegram), 4:20 PM ET tally + journal + Brier record, Friday
  4:45 PM ET distribution review draft. One Pine Script (v6) emitting the five fast-lane
  triggers (RVOL ≥2× time-of-day normalized, ORB break with volume, VWAP reclaim/loss,
  first-hour continuation, range compression) with a webhook setup guide. Ryan's setup steps
  documented (secrets → Supabase dashboard, `/start` to the bot, alerts on his symbol list).
- **Acceptance:**
  1. `select * from saa.triggers` shows a row created by a TradingView-format POST to
     `tv-webhook` (test POST sent from inside Postgres via `net.http_post`), and a matching
     open row in `saa.shadow_trades`. A POST with a wrong secret returns 401 and inserts nothing.
  2. Inserting a row into `saa.outbox` results in `sent_at` set (and, once the bot token is in
     place, a message in Ryan's Telegram DM). Before the token exists the row stays `pending`
     with the error recorded, never lost.
  3. `market-data` mode=calendar writes one `saa.calendar_days` row for the next trading day
     with earnings names and VIX/VIX1D/VIX3M (or a recorded error if a feed is down);
     mode=quotes writes `saa.price_ticks` only inside 9:28–16:02 ET.
  4. `shadow-scorer` closes an open shadow trade given ticks, writing `exit_reason`, `r_result`,
     `mfe_r`, `mae_r`; a unit test in `src/tests/` reproduces the modeled option price and
     trail logic for a known path.
  5. The 7:40 AM brief scheduled task, fired manually, writes one `saa.briefs` row and ≥1
     `saa.checklists` row with a probability, and queues a ≤5-line `saa.outbox` message.
  6. Foundry `validate` passes; everything is committed and pushed.
- **Gate to start M2 (observed, not built):** 10 consecutive trading days of complete records
  (brief + checklists + scored shadow trades) and Ryan reads them in ≤10 min/day. First dry
  run Monday 2026-09-28 7:40 AM ET.

### M2 — Daemon core: data plane (plan M2, part 1)
- **Builds:** Python 3.11 daemon `src/daemon/` — tastytrade OAuth2 (sandbox first), DXLink
  streaming for quotes/greeks, 1-min bars for SPY/QQQ/XSP + the day's watchlist, option chain
  snapshots (OI, volume by strike, IV), Nasdaq halt RSS poller, Cboe VIX term structure,
  OI-based dealer-gamma proxy; SQLite state; 9:25 AM heartbeat and 4:20 PM tally to Telegram;
  mirrors to `saa.*` tables. systemd unit + `.env` loading from the Seeking Alpha Agent folder.
- **Acceptance:** a full sandbox session run (9:25–4:15 ET) logs heartbeat, ≥380 one-minute
  bars per index symbol, ≥1 chain snapshot per active symbol every 5 min, zero unhandled
  exceptions; `pytest src/tests/` green; `saa.run_log` shows the run.

### M3 — Rules engine + sizing + shadow ledger with real marks (plan M2, part 2)
- **Builds:** six-gate decision (STAND-DOWN default), window scheduler (9:30–10:00 primary,
  event windows, 3:00–4:00 only on negative-gamma read, no fresh entries 11:30–1:30), theta-clock
  time stops, Kelly module (`size = k·f*(posterior p, W)`, shrinkage toward breakeven, k = 0.5,
  one-contract floor ≤ $100–150 while account < $2k, never above full Kelly), partial-bank at
  +7.5% of account then trail (30% of gain or one 1-min option ATR; 20% once ≥ +3R), mechanism
  exits, edge-loss halt (rolling-30 expectancy < 0 → floor; rolling-60 < 0 → stop), daily −3R
  stop, 3-loss lockout, cooling-off after ≥+5R win / ≥+8R day; shadow ledger scored at mid −
  half spread from DXLink marks; Tier 2 parameters in a versioned `saa.rules` table.
- **Acceptance:** property tests for every Tier 1 rail (each rail has a test that tries to
  breach it and fails); a replay of ≥5 recorded sessions through the engine produces a
  deterministic trade list (same input → byte-identical output); Kelly module unit tests
  reproduce the plan §0.3 table (25%/4R → 6%, 30%/5R → 16%, 35%/6R → 24%).

### M4 — Paper execution, approval mode, dashboard (plan M2, part 3)
- **Builds:** tastytrade sandbox order path (limit at mid, retry ladder, fill logging,
  reconciliation every 30 s, kill switch = file flag + `/halt`), Telegram approval flow
  ("Proposed: … Approve / Skip", 3-minute timeout = Skip), Next.js dashboard on Vercel
  (read-only: ledger, R histogram, expectancy, posterior p/W, Brier by bucket,
  required-vs-realized growth toward $5M by 2027-09-25).
- **Acceptance:** ≥3 sandbox round trips with fills reconciled; `/halt` flattens a sandbox
  position within 10 s; an approval that times out is logged as Skip; dashboard screenshot
  (Playwright) shows live sandbox data. **Gate to start M5 (observed):** ≥30 paper trades +
  shadow ledger, posterior expectancy > 0, avg loss ≈ −1R, ≥90% checklist compliance, zero
  Tier 1 breaches.

### M5 — Live, approval mode (plan M3)
- **Builds:** production OAuth, VPS deployment (Hetzner/DigitalOcean ~$6/mo, systemd,
  7:30 AM–4:15 PM ET), live-account sanity caps (max premium/contracts per order from account
  size), expiration-day liquidation policy verified with tastytrade, per-contract fee model.
- **Acceptance:** first live trade placed only after Ryan taps Approve; reconciliation matches
  the broker statement to the cent for 5 sessions; missing-heartbeat alert fires when the
  daemon is stopped deliberately. **Gate to start M6 (observed):** 20 live trades, no execution
  incidents, posterior expectancy still > 0.

### M6 — Full auto (plan M4)
- **Builds:** approval bypass inside Tier 1; k ratchet (0.5 → 1.0) only at the Friday review
  while rolling-30 expectancy > 0 and calibration slope ≥ 0.5; Friday review proposes at most
  one Tier 2 change with evidence, applied only after Ryan's reply.
- **Acceptance:** a full unattended live session with trades reported; a forced Tier 1 breach
  attempt in sandbox is refused and logged; review-gate: status `review` until Ryan ships.

## Tech decisions

- **Stack:** M1 = Supabase (Postgres 17 + Deno/TypeScript Edge Functions + pg_cron + pg_net)
  for storage and plumbing, Pine Script v6 for TradingView triggers, Anthropic scheduled tasks
  for the judgment jobs. M2+ = Python 3.11 daemon (asyncio; `tastytrade` SDK for OAuth/DXLink),
  SQLite for hot state, Supabase for the ledger, Next.js on Vercel for the dashboard. Chosen
  because: the trading loop must be deterministic and long-running (a daemon, not a chat or a
  scheduled run), while M1 places no orders and so can be fully serverless. The cloud build
  container cannot reach Supabase/Telegram over HTTP (proxy allowlist), so scheduled Claude
  sessions talk to Supabase only through the Supabase connector, and all outbound calls
  (Finnhub, Cboe, Telegram) run inside edge functions.
- **Persistence:** Supabase project "Quant edge" (`zspbkcheounkwnpjkgrv`, us-east-1), schema
  `saa`; existing `public.*` tables untouched. Daemon hot state in SQLite (M2+).
- **Auth:** no anon access to `saa.*` (RLS on, no policies; service role only). `tv-webhook`
  authenticates by a body `secret` (TradingView cannot send headers) plus an optional
  TradingView source-IP allowlist. `telegram-send`, `market-data`, `shadow-scorer` require a
  JWT and are called by pg_cron/pg_net with the project's publishable key; none of them accept
  free text to send — `telegram-send` only flushes `saa.outbox`.
- **Integrations & keys (Ryan provides, never in chat):** `TELEGRAM_BOT_TOKEN`,
  `FINNHUB_API_KEY`, `TV_WEBHOOK_SECRET` → Supabase Edge Function secrets; tastytrade sandbox
  and production OAuth client id/secret/refresh token → `.env` in the Seeking Alpha Agent
  folder (M2/M5); `ANTHROPIC_API_KEY` → `.env` (M2). Telegram chat id is captured
  automatically when Ryan sends `/start`.
- **Deploy target:** M1 Supabase + Anthropic cloud (scheduled tasks); M2–M4 Ryan's Mac or the
  VPS in sandbox; M5+ VPS.

## Risks & unknowns

- **Hardest part:** an edge that is real after costs. Every hit-rate in the corpus is derived,
  not measured; the sizing module cannot leave the floor until M1/M2 measure p and W.
- **Modeled option prices in M1** (Black-Scholes on the underlying's tick path with a VIX1D /
  per-symbol daily-vol proxy) understate spread and IV-crush effects; real marks arrive with
  DXLink in M2. Shadow results before M2 are directional evidence only.
- **Finnhub free tier** has no economic calendar and no intraday candles; the brief uses web
  search for the day's macro releases and the scorer uses 1-min quote polling.
- **TradingView webhooks** cannot be created by API; Ryan attaches the indicator to his own
  symbol list. Latency of a few seconds is fine for open/event windows, not cascades.
- **Scheduled tasks** must reach the Supabase connector from a fresh session; verified by a
  manual fire in M1. pg_cron runs in UTC — functions self-gate on America/New_York time so DST
  needs no cron edits.
- **Corpus contradictions** the ledger must measure: dealer-gamma sign baseline, final-hour
  gamma cliff, event-entry timing (synthesis §8).
- **Broker facts to verify before M5:** tastytrade expiration-day liquidation policy for long
  0DTE in small accounts; per-contract fees.

## Maintenance plan

Weekly (Friday review session or upkeep run): `foundry validate`; `pytest src/tests/`; confirm
pg_cron jobs ran (`saa.run_log` has entries for every weekday); confirm the three scheduled tasks
fired (last_run SUCCEEDED); Finnhub/Cboe endpoints still answering (errors land in
`saa.run_log`); Telegram delivery lag; TradingView alert expiry (TradingView alerts expire —
open-ended alerts need Premium, otherwise renew); dependency bumps for the daemon (M2+). After
Nov 1 2026 (DST end) confirm the ET-gated functions still fire once per slot.

---

## Addenda

Post-lock changes append here as `## Addendum vN (<date>)` with their own goals,
acceptance criteria, and continued milestone numbering. The original spec above is
never edited after locking.
