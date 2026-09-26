# Intake Interview — Seeking Alpha Agent — Autonomous Intraday Options Agent

**Date:** 2026-09-26 · **Source doc(s):** `spec/original/build-plan-v0.4-seeking-alpha-agent.md`
(Ryan's build plan v0.4, itself drafted 2026-09-25/26 from the 46-file research corpus, the
Fuse proposal of 2026-09-08, and the Goals ledger; companion docs in the claude.ai Project:
`claude/corpus-synthesis.md`, `claude/build-log.md`).

## What the planning doc already answered

- **Purpose & user:** Ryan; an agent that trades intraday long options autonomously while he
  works; objective $1,000 → $5,000,000 in 12 months, aggressive, front-loaded (§0).
- **The arithmetic:** +3.44%/day compounded average; +8.9% growth per trade over ~96 trades;
  three edges (25%/4R → ≈$2k, 30%/5R → ≈$180k, 35%/6R → $5M) and why measuring p and W is
  step one (§0.1–0.3).
- **Design principle:** rules engine trades, Claude thinks; no LLM in the execution path (§1).
- **Architecture:** five blocks — data, pre-market brain, rules engine + rails, execution,
  ledger/dashboard/alerts (§2).
- **Rule tiers:** Tier 1 immutable rails (size formula, edge-loss halt, −3R daily stop, lockouts,
  no entries 11:30–1:30, spread filter, order caps, kill switch); Tier 2 learned (≥30 trades,
  one change/week, logged, reversible); Tier 3 discretionary with Brier scoring (§3).
- **Exit logic:** partial-bank at +7.5% of account, trail 30%/20% of gain or one option ATR,
  mechanism exits, theta-clock time stops (§4).
- **Frequency decision:** window count, not a percentage; full catalyst universe with liquidity
  filter; fast-lane hypotheses in the shadow ledger with a ≥60-trade promotion gate (§5).
- **Scaling plan** by account size (§5a). **Milestones and numeric gates** M0–M4 (§6).
- **Tools verdict:** tastytrade primary (PDT rule gone 2026-06-04), Alpaca paper only,
  TradingView Premium as trigger feed + hypothesis killer + review charts, Seeking Alpha
  human-read only, TrendSpider excluded, $0 data (§7).
- **Operating model:** Python daemon on a VPS from M2, M1 serverless (Supabase + scheduled
  tasks), Telegram for alerts/approvals, `/halt`, daily timeline, first dry run Monday
  2026-09-28 7:40 AM ET (§9).
- **Secrets policy:** `.env` in the Seeking Alpha Agent folder / Supabase secrets, never in
  chat (§10). **Session protocol:** Foundry as the engine, one milestone per chat (§11).
- **M0 decisions accepted on defaults** (2026-09-26): tastytrade margin account, one-contract
  floor ≤ $100–150, k = 0.5, full catalyst universe + SPY/QQQ/XSP, Telegram, $0 data.

## Questions asked & answers

### Round 1 (2026-09-26)

**Q1 — Supabase home:** Where should the agent's tables and edge functions live? The org has
two active projects (free-tier cap), so a third needs Pro.
**A:** "Quant edge" project (`zspbkcheounkwnpjkgrv`), new schema `saa`.
**Consequence:** All M1 tables live in schema `saa`; the existing `public.*` tables of the
earlier Quant edge system (watchlist, daily_reports, options_snapshots…) are never touched.
Edge functions and pg_cron jobs are namespaced `saa_*`. $0 added cost.

**Q2 — Goal 2 ruling:** The plan's setup checklist item 6 asks for the Goal 2 (Trading) ledger
ruling (RECOMMIT / REVISE / KILL).
**A:** KILL.
**Consequence:** Goal 2 as written (≤5 hrs/week, 100% journaled, "no new Trading.tools builds
until a 12-week journal streak") is retired; the agent build stands on its own with this spec
as its ledger. The journal streak still exists as the M1 → M2 gate, but as an engineering
gate, not a goals-ledger friction rule. Recorded in DECISIONS.md (D3).

**Q3 — Telegram delivery:** DM or a group/channel?
**A:** Direct message.
**Consequence:** `telegram-send` captures the chat id from Ryan's `/start` message and stores
it in `saa.settings`; no group setup.

**Q4 — TradingView launch symbols:** which symbol set gets the indicator + webhook alert?
**A:** Ryan picks his own list.
**Consequence:** The setup guide documents one alert per symbol and a recommended starter set
(SPY, QQQ, IWM + liquid mega-caps) but the list is Ryan's; the engine treats any symbol that
arrives on the webhook as in scope, subject to the liquidity filter recorded on the trigger.

### Round 2

Not needed — the plan answered the rest of the rubric.

## Rationale summary

This is a measurement-first trading system dressed as an aggressive one: the objective is
extreme ($1k → $5M), so the design puts every dollar of risk behind a size formula whose inputs
(hit rate, payoff) start shrunk to breakeven and un-shrink only as the ledger fills. M1 therefore
builds the journal and shadow ledger with zero capital and zero servers — Supabase holds the
data, edge functions do the plumbing, TradingView supplies fast-lane triggers, and three
scheduled Claude runs do the judgment work (brief, tally, Friday review). The daemon that
actually trades arrives in M2–M4 in sandbox, goes live with approvals in M5, and only goes
fully autonomous in M6 behind the plan's numeric gates. Ryan chose the free path everywhere
(shared Supabase project, $0 data), killed the old Goal 2 friction rule so this spec is the
ledger, and keeps Telegram as the only UI until the dashboard in M4.
