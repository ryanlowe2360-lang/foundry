# Autonomous Intraday Options Agent — Build Plan v0.4

*v0 drafted 2026-09-25 from Ryan's research corpus (46 files in `Desktop/Seeking Alpha Agent`), the Fuse proposal (2026-09-08), the Goals ledger, and current broker/regulatory facts. v0.1 same day: objective restated by Ryan as $1,000 → $5,000,000 in 12 months; sizing, halt rule, and rule-rewrite loop redesigned accordingly. v0.2 same day: trade frequency addressed — full catalyst universe replaces index-first; fast-lane hypotheses added to the shadow ledger.*

---

## 0. Objective and the numbers it implies (stated once)

**Objective (Ryan, 2026-09-25):** grow a $1,000 account to $5,000,000 in one year, trading aggressively, front-loading returns while the account is small (>7.5%/day is possible on individual days) and accepting that the daily percentage falls as the account grows. The agent may break conventional sizing rules and rewrite its own edge rules from evidence.

### 0.1 Arithmetic

- ×5,000 over 252 trading days = **+3.44%/day compounded average**.
- Front-loaded path: 7.5%/day → $100k in ~64 trading days, $1M in ~96 days (≈4½ months); then ~1.0%/day for the remaining ~156 days takes $1M → $5M.
- The corpus finds **6–10 tradable windows a month** (≈96 trades/year). ×5,000 over 96 trades = **+8.9% account growth per trade, geometric average**.

### 0.2 Definitions (accountant-level)

- **R** — the dollars at risk on one trade (the premium paid for a long option, since the worst case is a full loss).
- **Hit rate (p)** — fraction of trades that win. **Payoff (W)** — average win expressed in R.
- **Expectancy** — average result per trade = p·W − (1−p)·1, in R.
- **Kelly fraction (f\*)** — the fraction of the account to risk per trade that maximizes long-run compound growth for known p and W: **f\* = p − (1−p)/W**. Half-Kelly = f\*/2 (about 75% of the growth, far smaller swings).
- **Growth per trade** (geometric, i.e., what compounds) = p·ln(1+f·W) + (1−p)·ln(1−f).
- **0DTE** — an option expiring the same day. **Theta** — the daily cost of holding an option (depreciation). **Dealer gamma** — whether market-makers' hedging damps or amplifies moves. **Spread** — bid-ask gap, a fixed cost per round trip. **Brier score** — how well stated probabilities match outcomes.

### 0.3 Three edges, all inside or at the top of the corpus's own derived ranges

| Edge (p, W) | Kelly f\* | Growth/trade | After 96 trades |
|---|---|---|---|
| 25% hit, 4R | 6% of account | +0.7% | ≈ $2,000 |
| 30% hit, 5R | 16% | +5.4% | ≈ $180,000 |
| 35% hit, 6R | 24% | +13.4% | $5M by trade ~64 (≈8 months); half-Kelly (12%) → +10.7%/trade → $5M in ~80 trades (≈10 months) |

- The difference between $2k and $5M is p moving 25→35% and W moving 4→6. At ~100 trades the hit-rate estimate is still ±5 points, so **measuring p and W is the first step of the $5M plan, not a brake on it** — they are the inputs to the size formula.
- Payoff shape of the full-Kelly $5M path: a 5-loss streak (probability 0.65⁵ = 11.6% per 5-trade stretch) costs −75% of the account; over 96 trades at least one such streak occurs with ~98% probability. Half-Kelly: the same streak costs −47%. This dictates the halt rule in §3.
- Corpus reference figures (all self-labelled *derived, not measured*): 15–30% hits at 3R+; 8–20% at 4–30×; 25–35% at 4–9×; +0.3 to +0.7R per trade; "full loss is the modal outcome on any single leg."

### 0.4 Ledger note

Goal 2 (Trading) in Ryan's Goals ledger: ≤5 hrs/week, 100% journaled, friction rule "no new Trading.tools builds until a 12-week journal streak." As of 2026-09-25 `trade_journal.csv` is header-only and the 8/19 RECOMMIT/REVISE/KILL ruling is unruled. Reconciliation: **Milestone 1 of this build is the journal** (checklist + Brier record daily, no orders); Claude does the engineering in Cowork sessions; Ryan's share is decisions and review, ≤1 hr/week. If the $5M-by-2027-09-25 objective enters the ledger it needs a weekly indicator: realized vs. required growth per trade. Ruling pending.

---

## 1. Design principle: the rules engine trades, Claude thinks

- **Rules engine** (Python, deterministic): reads data, evaluates the six gates, sizes by the Kelly module, places and manages orders, enforces every immutable rail. No language model in the execution path (slow, non-deterministic, unauditable).
- **Claude** (slow cadence, high judgment): 7:30–9:00 AM pre-market brief that fills the checklist with cited facts and a probability estimate; event-window regime reads; post-trade journal entries; the Friday distribution review that proposes edge-rule changes; code iteration. The corpus is Claude's rulebook.
- **Autonomy ladder:** paper → approval mode (agent proposes via Telegram, Ryan taps approve) → full auto. Each rung has a numeric gate (§6).

## 2. Architecture (five blocks)

| Block | What it does | Tech |
|---|---|---|
| **Data** | Broker streaming quotes/greeks (tastytrade DXLink); 1-min bars for SPY/QQQ/XSP/SPX + watchlist; option chain snapshots (OI, volume by strike, IV); econ + earnings calendar; Nasdaq trade-halt RSS; VIX term structure (Cboe); self-computed dealer-gamma proxy from OI | Python, tastytrade Open API, Finnhub free tier, Cboe/Nasdaq public feeds |
| **Pre-market brain** | Stage 0–4 funnel from `mode-b-morning-funnel-tail-candidates`: regime 2×2 → dated-catalyst gate → rank by historical event move ÷ implied move → flow bump → attention tiebreak. Output: signed checklist (6 gates, 15-item arming score) + probability for Brier scoring | Claude API + corpus as system context |
| **Rules engine + rails** | Six-gate decision (STAND-DOWN default); window entries (9:30–10:00 primary, event windows, 3:00–4:00 only on negative-gamma read); theta-clock time stops; Kelly sizing module; partial-bank then trail; mechanism-based exits; shadow ledger | Python, single process, state in SQLite |
| **Execution** | Limit at mid, retry ladder, fill logging, reconciliation every 30 s; kill switch | tastytrade Open API (sandbox first), Alpaca paper as second harness |
| **Ledger + dashboard + alerts** | Every trade and every shadow trade with the pre-strike checklist (timestamped, immutable), R result, hold time, exit reason, tag; Brier calibration; posterior p and W; required-vs-realized growth per trade toward $5M by 2027-09-25 | SQLite → Supabase; Next.js on Vercel (dashboard only); Telegram bot |

**Runtime:** $6/mo VPS (or a home Mac mini), systemd service 7:30 AM–4:15 PM ET. Never Vercel serverless for the trading loop.

## 3. Three tiers of rules

### Tier 1 — Immutable (survival physics; not overridable by Claude or by Ryan mid-session)
- **Size formula fixed, inputs learned:** size = k × f\*(posterior p, posterior W), with k between 0.5 (half-Kelly) and 1.0 set at M0 and changeable only at a weekly review. Posterior p and W start at the corpus prior **shrunk heavily toward breakeven** (n = 0 → size as if edge is barely positive) and un-shrink as the ledger grows. Floor: one contract ≤ $100–150 premium while the account is < $2k (whole contracts only; 0.25R adds are inexpressible at $1k). Ceiling: never above full Kelly of the posterior.
- **Edge-loss halt (replaces v0's −30% drawdown halt, which would stop the $5M path on its first ordinary losing streak):** if rolling 30-trade expectancy < 0, size drops to the floor until the rolling figure is positive again; if rolling 60-trade expectancy < 0, trading stops pending a written review.
- Daily stop −3R; lockout after 3 straight losses that day; cooling-off after any ≥+5R win or ≥+8R day (next session at half size).
- No fresh entries 11:30 AM–1:30 PM; every entry carries a time stop at its window edge; never own an option through a scheduled release (FOMC 1:45–2:00, CPI/NFP 8:30).
- Spread filter: skip above 10% of premium; flag 5–10%.
- Order sanity caps: max premium per order and max contracts per order set from account size; kill switch = file flag + Telegram command that flattens and halts.

### Tier 2 — Learned (edge rules; the "rulebook that rewrites itself")
Checklist item weights, the 15/28 arming thresholds, entry windows, instrument set, trail parameters, partial-bank level, catalyst classes allowed. Updated by the Friday review from the ledger: **no change on fewer than 30 relevant trades (real + shadow), one change per week, each change logged with the evidence and reversible.** Bayesian framing: corpus priors → posterior as data arrives.

### Tier 3 — Discretionary (Claude's pre-market judgment)
Regime read, catalyst classification, direction call. Free-form but every call carries a probability and is Brier-scored; a calibration slope < 0.5 over 30 calls reduces Tier 3's weight in the gates.

## 4. Trailing-stop / let-winners-run logic

- Partial-bank trigger: when open P&L reaches **+7.5% of account**, sell enough to return the original premium to cash where contract count allows (with one contract the whole position trails instead); trail the remainder.
- Trail: stop = high-water mark of the option's mark minus max(30% of the gain, one 1-min ATR of the option); tighten to 20% once gain ≥ +3R. (Trail parameters are Tier 2 — learned.)
- Mechanism exits override the trail: cut on mechanism contradiction (pushes sold into, volume tapering), on loss of VWAP for single-name gappers, at the theta-clock time stop.
- Past-the-bell runner only when checklist item 10 (day-2 setup) fires — on 1–3 DTE, not 0DTE.

## 5. Trade frequency: widen the universe, don't lower the bar (v0.2)

**Question raised (Ryan, 2026-09-25):** the corpus fires 6–10 windows a month; should the agent target 3.44%/day with a faster-moving signal set instead?

**Finding:** trade count multiplies per-trade edge in either direction. A fast directional scalp on a 0DTE option with a symmetric +40%/−40% target/stop and ~7% round-trip cost (spread + fees on a $1–2 contract) has breakeven hit rate (40+7)/80 = **58.75%**. The corpus's measured directional frameworks reach 55–58% before option costs; free-data technical confluence 52–55%. At 55%: 0.55×40 − 0.45×40 − 7 = −3% of premium per trade → at 10% of account per trade and 60 trades/month, **−18%/month** (52% → −32%/month; 60% → +6%/month). Leverage (the option's large % ATR) scales whatever edge exists; it does not create one.

**The corpus's own frequency evidence:** opening-range breakout −0.02R/trade below 100% relative volume, +0.08R above, +0.38R above 3,000% (`Conditional Call Inputs .md`) — fast signals pay only when the name is in play, i.e., the catalyst gate again.

**Design decision — the daily objective is a count, not a percentage:** find and gate every catalyst window today, across the whole market.

- **Universe:** every US name with a dated catalyst that session — earnings (dozens/day for ~8 months of the year), PDUFA/FDA dates, macro releases, index events, LULD halts, qualified gappers — filtered for a liquid chain (spread ≤5% of premium preferred, hard skip >10%). Same Stage 0–4 funnel and six gates. Expected output 20–40 windows/month, plus 2–3 concurrent whole-contract positions on armed days.
- **Same edge, more draws** (half-Kelly, corpus mid-case 30% hit / 5R → +4.3% per trade): 8 trades/month → ×1.41/month; 30 trades/month → ×3.6/month → $5M in ~7 months. A 55%-direction fast lane at 60 trades/month → ×0.82/month.
- **Fast-lane hypotheses live in the shadow ledger from M1, day one:** opening-range breakout on in-play names, VWAP reclaim, first-hour continuation — each expressed as an option trade, scored at mid minus half the spread with zero capital. Promotion to real capital only after ≥60 shadow trades with expectancy > 0 after modeled costs. This is the rulebook rewriting itself on evidence; an unproven lane costs nothing.
- The 3.44%/day appears on the dashboard as required-vs-realized only. It is never a quota — a quota is an instruction to trade with no signal.

## 5a. Scaling plan (what changes as the account grows)

| Account | Instruments | Why |
|---|---|---|
| < $25k | full catalyst universe (single names through the catalyst gate) + SPY / QQQ / XSP for index windows | most windows per month; whole-contract sizing on cheap chains |
| $25k–$250k | SPX replaces SPY for index windows | cash-settled, no assignment, Section 1256 (60% long-term / 40% short-term) — roughly a 10-point effective-rate difference at the top bracket; index 0DTE liquidity is far beyond $5M |
| > $250k | multiple concurrent independent windows and instruments; single-name size capped by chain depth | raises trades per month (n), the real constraint on both measurement and compounding; capacity binds first in single names, not indices |

**Shadow ledger:** every setup the gates fire on — and every fast-lane hypothesis firing — is scored at mid minus half the spread whether or not it was taken. Grows the sample 3–5× faster than taken trades alone (the Fuse "Proving Ground" layer).

## 6. Milestones with numeric gates

| # | Scope | Gate to proceed |
|---|---|---|
| **M0 — Decisions** (this week, ≤1 hr) | Broker = tastytrade margin account (PDT rule gone 6/4/2026); size floor = one contract ≤ $100–150; k = 0.5 to start; universe = full catalyst universe with liquidity filter + SPY/QQQ/XSP; alerts = Telegram; data budget = $0 | Ryan confirms or overrides |
| **M1 — Proving Ground (journal only)** (weeks 1–2) | Pre-market brief + auto-filled checklist + Brier record + shadow ledger (corpus windows and fast-lane hypotheses) every trading day; no orders. Produces the first p and W estimates. Fulfills Goal 2's journal streak from day 1 | 10 consecutive trading days of complete records; Ryan reads them in ≤10 min/day |
| **M2 — Paper execution** (weeks 3–6) | Full engine in tastytrade sandbox / Alpaca paper; approval-mode alerts; dashboard live with required-vs-realized growth | ≥30 paper trades + shadow ledger; posterior expectancy > 0; avg loss ≈ −1R; ≥90% checklist compliance; zero Tier 1 breaches |
| **M3 — Live, approval mode** (weeks 7–10) | Real money, size from the Kelly module at k = 0.5; Ryan approves each entry from his phone | 20 live trades, no execution incidents, posterior expectancy still > 0 |
| **M4 — Full auto** | Agent trades without approval inside Tier 1; k may rise toward 1.0 at weekly review only while rolling 30-trade expectancy > 0 and calibration holds | Continues while the edge-loss halt is not triggered |
| **Ongoing** | Friday 60–90 min distribution review (Claude drafts): R histogram, expectancy, avg win/loss, % of profit from top 3 trades, Brier by bucket, one Tier 2 change max | — |

## 7. Tools verdict

- **tastytrade (primary broker):** account exists; Open API (OAuth2, sandbox, DXLink streaming, options orders); adopted FINRA's intraday-margin framework on day one (6/4/2026) — a $1k margin account is no longer PDT-restricted. **Verify before M3:** expiration-day liquidation policy for long 0DTE options in small accounts; per-contract fees (~$1 to open, $0 to close, plus clearing/regulatory cents — ~1–2% round trip on a $100 contract).
- **Alpaca (paper harness / backup):** options + 0DTE via API, paper supports options, commission-free; but expiring-option orders stop at 3:15 PM ET and expiring positions auto-liquidate at 3:30 PM ET, which kills the corpus's final-hour plays — paper/backup only.
- **TradingView Premium — three jobs (v0.3):** (1) M1–M2 trigger feed for the fast-lane hypotheses: Pine Script alerts for time-of-day-normalized RVOL ≥2×, opening-range break with volume confirmation, VWAP reclaim/loss, first-hour continuation, and the corpus's range-compression flag, delivered by webhook into the shadow ledger; (2) hypothesis killer — strategy tester on the *underlying* over years of intraday history, run before any hypothesis gets a shadow-ledger slot (cannot price the option, so it filters, never proves); (3) Friday review charts. Limits: not the options data source (tastytrade DXLink is), and webhook latency of a few seconds — acceptable for open/event windows, not for cascade plays. Build order: webhooks in M1–M2; the engine computes the same triggers natively from 1-min broker bars in M3+ so nothing external sits in the live path.
- **Seeking Alpha — one narrow job (v0.3):** catalyst-quality reads in the pre-market brief for that day's earnings names (beat/miss, guidance direction, already-priced?), read by Claude through Ryan's logged-in browser as a human would — no automated scraping (ToS). Feeds funnel Stages 1–2. Per `14-seeking-alpha-momentum-framework-bonus.md` the quant ratings are a weekly swing signal with no API and UI-only factor grades; a swing sleeve, if ever wanted, is separate from the intraday agent.
- **TrendSpider:** not needed; nothing in the corpus requires it.
- **Data:** $0 to start (broker streaming + Finnhub free + public Cboe/Nasdaq feeds). ThetaData only if we choose to backtest rather than forward-test; the corpus's instruction is "fit the thresholds to your trade log."
- **Claude API** ~$10–20/month; **VPS** ~$6/month.

## 8. Open questions carried from the corpus (the agent's first research targets)

1. True dealer-gamma sign baseline (docs disagree; vendors disagree) — the engine logs its OI-based proxy vs. realized behavior.
2. Whether the final-hour 0DTE "gamma cliff" amplifies or dampens (Mode B vs. Mode A) — measured by window.
3. Event-entry timing before vs. after releases — Tier 1 chooses *after*; the shadow ledger records what pre-release entries would have done.
4. Every hit-rate and payoff number in the corpus is unmeasured — M1/M2 exist to measure them, and the sizing module cannot go above the floor until they are.

## 9. Operating model (decided 2026-09-26)

**Who places trades:** a Python **trading daemon** Ryan owns, running on a machine that stays on during market hours. Not a chat session (not running unless he's in it) and not a scheduled task (runs minutes, not hours). Claude-the-model is a component the daemon calls through the API for slow judgment jobs (catalyst quality, regime read, journal text). Claude-in-Cowork is the engineer: builds, maintains, reads logs in build sessions, proposes Tier 2 changes on Fridays. Ryan's operating burden: read the 7:40 brief (optional), watch Telegram, tap Approve/Skip in M3, `/halt` if ever needed, one Friday review.

| Piece | Where | When | Human needed |
|---|---|---|---|
| Trading daemon (data stream → gates → sizing → orders → management → Telegram) | VPS ~$6/mo (M1–M2 may run on Ryan's Mac; M1 needs neither) | 7:30 AM–4:15 PM ET every trading day, self-starting | No (M3: approvals; kill switch) |
| Claude API calls inside the daemon | Same machine | Pre-market and event windows | No |
| Scheduled Claude tasks (Friday review draft, ledger log) | Anthropic cloud | Friday 4:45 PM | No |
| Claude in Cowork (build/maintain) | Chat | When Ryan opens it | Yes, ≤1 hr/week |

**Daily timeline:** 7:40 AM brief (5 lines) → 9:25 heartbeat ("online, feed OK, buying power $X"; missing heartbeat = down) → 9:30–4:00 loop every second, messages only on events (fill, bank, trail exit, stop, daily stop, halt) → 4:20 PM tally (trades, R, hit rate to date, required-vs-realized growth) → Friday 4:45 review draft. **Approval mode (M3):** "Proposed: buy 1 XYZ 0DTE 52C @ $0.95 — 6/6 gates, arming 31/56 — Approve / Skip"; 3-minute timeout, silence = Skip; windows are known in advance (open, event times, last hour). **M4:** trades and reports fills. **Kill switch:** `/halt` flattens and stops until manual restart.

**M1 architecture (no VPS, no Mac uptime):** Supabase project — tables `checklists`, `triggers`, `shadow_trades`, `calibration`; edge function `tv-webhook` receiving TradingView alerts; edge function `telegram-send`; secrets held in Supabase, never in prompts. Pre-market brief = scheduled task 7:40 AM ET weekdays (Finnhub calendar → Stage 0–4 funnel → six gates → checklist with probability → Telegram). End-of-day scorer = 4:20 PM ET (closes shadow trades on the underlying's path with a modeled option price; real option marks arrive in M2 with DXLink). Friday review draft = 4:45 PM. First dry run: Monday 2026-09-28, 7:40 AM ET.

## 10. Ryan's setup checklist (before or during the first build session)

1. tastytrade developer portal (developer.tastytrade.com): create an OAuth application, generate a refresh token, open a sandbox account. Client secret + refresh token → password manager → `.env` in the Seeking Alpha Agent folder. Never in chat. Confirm the account's options tier allows buying calls and puts.
2. Telegram: @BotFather → `/newbot` → token into `.env`; share only the bot username.
3. Finnhub free API key (finnhub.io) → `.env`.
4. Claude API key → `.env` (for the daemon's judgment calls).
5. For M2+: VPS account (Hetzner or DigitalOcean, ~$6/mo); exact clicks supplied at M2.
6. Goal 2 ruling: M1 counts as the journal from day one; REVISE (zero-line weekly indicator, cap unchanged) is the consistent choice — say the word and it gets logged.

## 11. Session protocol (how nothing gets lost)

- **Source of truth is files, not chat.** Spec: this doc. Research: `claude/corpus-synthesis.md` (the full read of the 46-file corpus, so no session re-reads 127k words). State: `claude/build-log.md` (what's built, what's verified, next action, open questions) — every build session reads it first and updates it last. Code: a git repo (Foundry `projects/<slug>/src/` or the Seeking Alpha Agent folder).
- **One milestone per chat, all inside this Project** so the docs and project memory are present on every open. Name chats "M1 build", "M2 build", etc.
- **Ryan's Foundry is the intended engine:** `foundry:intake` on this doc locks the spec with acceptance criteria per milestone; `foundry:build` executes one milestone per session with verification evidence and the end-of-session ritual (state touch, buildlog, commit, push); `foundry:resume` picks up from the exact resume point. Foundry lives in a git repo; the first session needs its URL or a GitHub connection.
- **Every session ends with:** build-log updated, code committed, next action written as "file, function, what's left, how to verify."
- The 46-file corpus stays on Ryan's Mac; sessions that need the raw files select that folder. Most won't — the synthesis doc carries the numbers and the contradictions.

## 12. Change log

- **v0.4 (2026-09-26):** operating model (who places trades: the daemon; Claude API inside it; Cowork as engineer), daily timeline, M1 serverless architecture, Ryan's setup checklist, session protocol; defaults accepted (tastytrade margin, one-contract floor, half-Kelly, full catalyst universe, Telegram, $0 data); first dry run Monday 2026-09-28.
- **v0.3 (2026-09-26):** tool roles specified — TradingView Premium as M1–M2 webhook trigger feed, hypothesis killer on the underlying, and review charts; Seeking Alpha limited to human-read catalyst-quality checks in the pre-market brief; TrendSpider still excluded.
- **v0.2 (2026-09-25):** frequency question answered — the daily objective is a window count, not a percentage; universe widened to every catalyst-bearing name with a liquid chain (expected 20–40 windows/month vs 6–10); fast-lane hypotheses (ORB in-play, VWAP reclaim, first-hour continuation) enter the shadow ledger at M1 with a ≥60-trade, expectancy > 0 promotion gate; breakeven arithmetic for fast signals recorded (58.75% at ±40% / 7% cost).
- **v0.1 (2026-09-25):** objective restated to $5M in 12 months; fixed 1R replaced by learned fractional-Kelly sizing with shrinkage; −30% drawdown halt replaced by edge-loss halt; three-tier rule structure (immutable / learned / discretionary) with sample-gated rewrites; shadow ledger; instrument laddering by account size; required-vs-realized tracker on the dashboard.
- **v0 (2026-09-25):** initial plan.
