# Foundry Ledger

*Generated 2026-10-03T23:59:00Z by `scripts/foundry.py ledger` — do not edit by hand.*

## 🙋 Needs you

- **Seeking Alpha Agent — Autonomous Intraday Options Agent** (`seeking-alpha-agent`) — **question:** M4 live evidence (Ryan, from the Mac, v0.4.3): on a trading day inside 09:30-16:00 ET with ./run.sh session stopped — ./run.sh paper-roundtrip --n 3 --allow-delayed, ./run.sh halt-test --allow-delayed (needs <= 10 s), ./run.sh approval-test twice (let one time out, Approve the other); paste the output. Milestone 4 cannot close without them.
- **Seeking Alpha Agent — Autonomous Intraday Options Agent** (`seeking-alpha-agent`) — **question:** Market-data entitlement (D19): the production DXLink feed is 15+ minutes delayed on the API quote token (level api) even with the account funded. Ryan asks tastytrade support for real-time data on the API streamer; engine and paper path stay observe-only until it is real-time.
- **Seeking Alpha Agent — Autonomous Intraday Options Agent** (`seeking-alpha-agent`) — **question:** Dashboard (feature change, Ryan decides): saa_dashboard sums every closed saa.paper_trades row into the paper realized line, the R histogram and the closed count — including CLI self-test rows (lane roundtrip / halttest) and the sandbox's rule-based fill prices (under $3 at the daemon's own limit, market at $1; D25). Keep as is, or separate self-tests and label sandbox fills (migration 0009 + a self-tests count in the paper panel)?

## 🔨 In flight

| Project | Status | Milestones | Idle | Next action |
|---|---|---|---|---|
| **Pocket Notes** (`pocket-notes`) *(example)* | building 🔥 | 2/4 done | 94d | Start M3: add an &lt;input id="search"&gt; above the note list in src/index.html and filter renderList() by t… |
| **Seeking Alpha Agent — Autonomous Intraday Options Agent** (`seeking-alpha-agent`) | building | 3/6 done | 0d | Close M4 (walkthrough in progress; daemon v0.4.3 on the Mac): grade Ryan's ./run.sh smoke on v0.4.3, record t… |

---
*2 project(s) · 2 in flight · 1 stale (≥7d) · 0 unprocessed inbox doc(s).*
