# Decisions — Seeking Alpha Agent — Autonomous Intraday Options Agent

Lightweight decision log. Newest on top. Record anything a future session (or future
Ryan) would otherwise re-litigate.

## D24 (2026-10-03) — The sandbox self-tests pick the instrument from the intersection of both chains and dry-run it in the sandbox first

- **Context:** Ryan's first `./run.sh paper-roundtrip --n 1 --allow-delayed` (Saturday, 15-min-delayed quote) chose
  production's nearest SPY expiration (Mon 2026-10-05, strike 770) and the sandbox refused the order with
  `instrument_validation_failed: Trading of SPY   261005C00770000 is not supported`. Not a permissions problem (the
  smoke test shows the account, the options level and 0 positions / 0 live orders fine): the cert environment carries its
  own, smaller and sometimes stale instrument universe, and its order router validates against *that*, not against the
  production chain the engine and the quotes come from.
- **Options:** (a) hard-code a "known good" contract — rots in days; (b) retry blindly through strikes/expirations —
  slow and opaque; (c) choose from what both environments know and ask the sandbox before placing.
- **Chose:** (c). `paper.option_candidates` fetches the production chain (quotes, the engine's view) and the sandbox's
  own `NestedOptionChain` (via the sandbox session), keeps live expirations present in both with strikes present in both,
  orders nearest-expiration-first / nearest-ATM-first (3 strikes per expiration, 6 candidates), then `choose_entry` makes
  each candidate pass: the sandbox instrument lookup (`Option.get` — unknown / inactive / closing-only → skip), a two-sided
  production DXLink quote, and a **sandbox dry run of the exact entry order** (`TastytradeBroker.dry_run`, 1 contract
  buy_to_open at the ladder's first rung). Every step is a printed line and lands in the `saa.run_log` row (`pick`), so a
  refusal is a diagnosis ("sandbox chain: 0 live expirations of 12 listed …"), never a mystery. The sandbox chain being
  unreadable falls back to the production chain and says so. The engine/executor path is untouched: this is the
  self-tests' instrument choice only; the live engine trades what the brief + chain plan say, and in M5 the production
  router validates against the same chain the quotes come from.
- **Revisit if:** the sandbox proves to know none of production's live expirations for SPY on a trading day (then the
  evidence runs use `--symbol QQQ` / `AAPL`, and the spec's "sandbox round trips" may need a different underlying than the
  engine's), or when M5 adds the production broker (dry runs there become the pre-trade check of the real order path).

## D23 (2026-10-02) — The dashboard is server-rendered off one RPC with the service-role key; no anon access, access key in front

- **Context:** `saa.*` has RLS on with no policies (service role only, D13); a browser app would need either anon policies or
  a backend. The spec wants a read-only Next.js page on Vercel.
- **Chose:** Next.js 15 App Router, dynamic server component, one `POST /rest/v1/rpc/saa_dashboard` with the service-role key
  from a server-side env var (never `NEXT_PUBLIC_`), middleware gate on `DASHBOARD_ACCESS_KEY` (`?key=` once, then a cookie).
  `saa_dashboard(p_days)` (migration 0008) assembles the whole document in Postgres (ledger with paper joins, stats by
  source, R vectors, Brier, paper panel, daemon run, today). The posterior / growth arithmetic is ported to TypeScript
  (`lib/posterior.ts` = kelly.py; `lib/growth.ts` with the NYSE calendar) for display only — the daemon stays the source of
  truth. Charts are pure SVG with native tooltips and table views (dataviz palette, ≤ 3 series, legend always).
  `SAA_DASHBOARD_FIXTURE` renders a saved document (screenshots, offline review). The Vercel deploy itself is Ryan's step
  (his account); `deploy_url` goes into STATE when it exists.
- **Revisit if:** more than one reader needs it (then real auth), or the document grows past a comfortable single RPC
  (then split reads / cache).

## D22 (2026-10-02) — M4 acceptance evidence comes from three CLI self-tests run on the Mac; `--allow-delayed` is an explicit, recorded exception

- **Context:** the cloud build box and the linked Mac shell cannot reach tastytrade, so sandbox round trips, the halt test
  and a real Telegram proposal can only run from Ryan's terminal. The production feed is still delayed (open question),
  so the strict D19 gate would make the plumbing untestable until tastytrade fixes the entitlement.
- **Chose:** `./run.sh paper-roundtrip [--n 3]`, `halt-test`, `approval-test` — each runs the production code path
  (`OrderManager.work/flatten`, `Executor.halt`, `ApprovalGate` + `TelegramBot.poll`) against the real sandbox / Bot API and
  writes a `saa.run_log` row (`paper:roundtrip`, `paper:halt_test`, `paper:approval_test`) plus `saa.paper_trades` /
  `saa.paper_orders` / `saa.approvals` rows, so the evidence is in the database, not in a chat. The entry quote is probed
  from production DXLink and the feed lag measured first; if it is not real-time the tests refuse unless `--allow-delayed`,
  which is loud and recorded (`allow_delayed`, `feed.mode`) in the row — a sandbox fill is plumbing evidence, never a
  decision. The approval test keeps `daemon_last_seen` fresh so the edge function leaves `getUpdates` to it (D20).
- **Revisit if:** tastytrade's real-time entitlement arrives (then drop the flag from the instructions) or a paper harness
  with real fills (M5 VPS) makes the sandbox tests redundant.

## D21 (2026-10-02) — Execution policy: D19 gate on the order path, Tier 1 caps re-checked at the order, approval for entries only, no auto mode before M6

- **Context:** the engine already refuses to fire on a delayed feed and sizes inside the Tier 1 caps, but the executor is a
  second actor with its own failure modes (a stale approval, a mark that moved while Ryan thought, a stranger position).
- **Chose:** (a) every gate-fired open is re-checked by the executor: kill switch → `feed_lag.mode == realtime` (D19, belt
  and braces) → Tier 1 order caps at the engine's ask, and again at the **live** ask after approval (≤ 2 contracts per $1k,
  ≤ 50 % of the account) → `blocked` / `refused` rows, never an order. (b) Entries need Approve; **exits never do** — a
  close, a bank or a halt is risk reduction. (c) Fast-lane hypotheses never trade. (d) `ExecutionPolicy.mode` is
  `approval`; `auto` raises until `ALLOW_AUTO_MODE` flips in M6. (e) Ladder: mid rounded toward the far side, 3 rungs of
  5 s to the far side, 5 s grace, cancel; a partially filled order is never replaced (tastytrade rejects it) — it rests and
  is cancelled at the end, the executor then closes the filled part. An exit whose ladder fails escalates to the flatten
  ladder (bid → bid − step at 3 s → market at 6 s). (f) `halt()` expires proposals, cancels working orders, flattens every
  open paper position **and any sandbox position the book does not know**, reports seconds to flat (budget 10 s; the
  simulations flatten in 6 s). The HALT file survives restarts; `saa.settings.halt` is kept in step so the M1 `/halt` path
  still works and a halted daemon starts halted. (g) Realized R = (proceeds − cost − fees) ÷ cost; slippage vs the
  engine's ask/bid recorded per trade so the shadow ledger's marks can be audited against fills.
- **Revisit if:** fills show the 5-second rungs too slow for 0DTE (then shorten / add a marketable-limit first rung), or
  M5's live account needs per-contract fees in the sizing (then the fee model lands in kelly.size_position).

## D20 (2026-10-02) — Telegram approvals: the daemon long-polls the Bot API itself; the `telegram-send` edge function stands down while the daemon is alive

- **Context:** M1's `telegram-send` edge function polls `getUpdates` (every 2 min and on every outbox insert) for `/start`,
  `/halt` etc. Telegram allows one `getUpdates` consumer per bot (409 Conflict otherwise), the outbox path cannot carry
  inline buttons, and a 3-minute approval needs second-level latency.
- **Options:** (a) a Telegram webhook into the edge function + the daemon polling Supabase for decisions every 2 s (more
  surgery on live M1 infra, needs `setWebhook` with the token, a permanent change of delivery mode); (b) the daemon polls
  directly and the function skips polling while `saa.settings.daemon_last_seen` is fresh (< 3 min), both sharing
  `saa.settings.telegram_update_offset`.
- **Chose:** (b). `execution/telegram_bot.py` sends proposals with Approve / Skip buttons, edits them with the decision,
  answers callbacks, handles `/halt /resume /status /positions /help /id`, honours only the owner's chat, backs off on 409.
  `telegram-send` v4 (deployed 2026-10-02 23:33 UTC, verified `ok` with 0 poll errors): `daemonAlive()` → `poll_skipped`,
  `allowed_updates` now includes `callback_query`, a tap that reaches it while the daemon is down is answered "expired".
  The daemon loads the shared offset at start and writes it back after every batch, so neither side re-reads the other's
  updates. The `/halt` typed while the daemon is not polling still lands in `saa.settings.halt`, which the daemon checks
  every reconciliation (30 s) and at start.
- **Revisit if:** a second bot or a group chat is wanted (then a webhook + router), or the 3-minute overlap at daemon start
  produces visible 409s (then bump `DAEMON_ALIVE_MS` or signal the hand-over explicitly).

## D19 (2026-10-01) — Live evaluation is gated on a real-time feed; everything else in M3 is testable offline

- **Context:** the production DXLink feed measured 15 minutes delayed on 2026-09-28 (open question, cause unconfirmed). A
  decision made on a 900-second-old quote is fiction; a shadow ledger marked on stale bids would mis-measure p and W.
- **Chose:** the engine takes `feed_lag.mode` as an input every tick. In a live session (`require_realtime=True`, not
  configurable from `.env`) it opens nothing — gate path or fast lane — unless the mode is `realtime` (median exchange→receipt
  delay of underlying trades < 30 s); it records one `feed_not_realtime` journal row per symbol per window and the heartbeat /
  EOD say "observe-only". Positions already open keep being managed on whatever marks arrive (closing is better than
  abandoning). Offline — tests, `replay --engine --eval` — the gate can be lifted, and a pre-M3 recording replays with the
  recording's own lag mode, so the whole engine (property tests, deterministic replay, Kelly table) is exercised without a
  live feed. M3 code is therefore complete while the entitlement question stays open; its *live* evidence waits on real-time data.
- **Revisit if:** tastytrade confirms the account entitlement is real-time (then the first live engine day is the evidence) or
  a different real-time source is adopted (then `MarketState.feed_lag()` is the only place that changes).

## D18 (2026-10-01) — Engine ticks once a minute on a recorded mark digest; live == replay by construction

- **Context:** the spec wants a replay of recorded sessions to be deterministic. D12 kept the recording at underlying-level
  events (option quotes are ~300 MB/day), so a replay had no option marks; and a trail evaluated on every 2-second quote
  cannot be reproduced from any recording that is not the full option tape.
- **Chose:** the engine decides on one **mark digest** per underlying per minute (the planned options within ±1.5 % of spot,
  `[bid, ask, iv, delta, gamma, theta, oi, volume, quote_ms, recv_ms]`), taken 2 s into the minute — after the previous bar
  completes — and written to the recording as `OptMarks` + `Tick` (feed mode, VIX, gamma proxies, universe) *before* the
  engine sees them; `Meta` (rules, checklists, account, k, history, cooling-off) and `Plan` (chains) are recorded at start.
  Replay feeds the same records in the same order to a fresh `MarketState` + `Engine`, so the live and replayed decisions,
  ledger and state are byte-identical (`test_engine_session_sim.py` proves it on a full simulated day). Cost: ~40 MB/day of
  digests on a 14-name universe; the trail reacts at most once a minute (bars are 1-minute; the theta clock is in minutes —
  the plan's exits are bar- and window-based, not tick-based). The HWM of the 2-second marks is *not* recorded.
- **Revisit if:** the ledger shows trail exits losing materially versus intraminute marks (then record a 15-second digest, or
  the HWM between ticks, and tick faster) or the recording size binds on the VPS disk.

## D17 (2026-10-01) — Engine inputs and window semantics: brief checklists are Tier 3, every in-session calendar event is a release

- **Context:** the six gates need a catalyst and a direction the daemon cannot measure (that is Claude's 7:40 job), and the
  Tier 1 clock rules need to know which scheduled releases the day holds.
- **Chose:** (a) today's `saa.checklists` rows (via `saa_checklists_today`) are the engine's Tier 3 input — gates 1 and 5
  (and 3–4 for single names) come from them; a symbol without a checklist stands down in every gate window; a `two_sided`
  checklist opens one contract per leg. The checklist for a window is matched by `window_start ≤ now < window_end`, else by
  `window_kind`. (b) Every event in `saa.calendar_days.econ` with an in-session time is a scheduled release: blackout T−15…T
  (flat, no entries), entries T+5…T+15, stop T+55, FOMC adds the presser window at T+30; a pre-market release (CPI/NFP 08:30)
  makes a data day (open-window entries from 09:35). The brief's own `econ` entries (e.g. a 10:30 regional Fed survey) are
  therefore blackouts too — conservative on purpose. (c) Fast lanes obey the Tier 1 clock (dead zone, blackouts, last 5 min)
  but not the day rails (daily stop, lockout): hypotheses keep measuring with zero capital; they never enter the posterior or
  the halt windows. (d) The gate path fires at most one position per symbol per window per day; a fast lane at most one
  shadow per symbol/lane/direction per day. (e) Alerts: gate-fired opens/closes only, ≤ 20/day (`saa.rules.max_alerts_per_day`).
- **Revisit if:** the Friday review wants minor calendar events excluded from blackouts (add a `kind` filter — the calendar
  rows already carry `kind`), or wants fast-lane shadows throttled differently.

## D16 (2026-10-01) — Tier 1 is code; Tier 2 is the versioned `saa.rules` table (v2 seeded by the build)

- **Context:** plan §3 says Tier 1 is "not overridable by Claude or by Ryan mid-session". The v1 rules row from intake
  mixed Tier 1 numbers (daily stop, lockout, cooling-off, dead zone, spread filter) with Tier 2 edge parameters, which would
  have let a Friday-review `saa.rules` insert change survival rails.
- **Chose:** `saa_daemon/engine/tier1.py` holds the rails as a frozen dataclass (k ∈ [0.5, 1.0], shrinkage n0 = 30, prior
  30 %/5R, ε = 0.5 %, floor while account < $2k and premium ≤ $150, caps ≤ 2 contracts per $1k and ≤ 50 % of account per
  order, edge-loss halt 30/60, daily −3R, 3-loss lockout, cooling-off ≥ +5R win / ≥ +8R day → half size, no entries
  11:30–13:30, release blackout 15 min, out by close − 5 min, spread 5 % flag / 10 % skip). `Rules.from_params` refuses
  those keys from data (logged, listed in the heartbeat). Migration 0007 inserts rules **v2** carrying only Tier 2 keys —
  every v1 edge value unchanged (bank 7.5 %, trail 30 %/20 % at +3R, 0.5σ OTM, lanes, promotion gate) plus the new
  engine parameters (windows, gate thresholds, mechanism exits, max concurrent 3, alert cap 20, mark window 1.5 %). This is
  a build change, not a review change (the Tier 2 policy of one evidence-based change per week is untouched). Changing a
  Tier 1 value is a spec addendum.
- **Revisit if:** M6's k ratchet (0.5 → 1.0 at a review) needs `kelly_k` to move — it lives in `saa.settings.kelly_k`,
  clamped to the Tier 1 range in code, which is the intended path.

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
