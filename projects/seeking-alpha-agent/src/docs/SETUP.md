# Seeking Alpha Agent — M1 setup (Ryan's part, ~15 minutes)

Everything below is the only human work M1 needs. Nothing here places an order; M1 is the
journal + shadow ledger. Secrets go into Supabase, never into chat.

## 1. Secrets into Supabase (2 min) — this is separate from the `.env`

The `.env` in the Seeking Alpha Agent folder feeds the M2+ daemon on your machine. The M1 edge
functions run inside Supabase and can only see **Supabase's own secrets store**, so the same two
values have to be pasted there once:

Supabase dashboard → project **Quant edge** → **Edge Functions** → **Secrets** → *Add new secret*:

| Name | Value | Where it comes from |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | the token @BotFather gave you (`123456:ABC…`) | your `.env` line `TELEGRAM_BOT_TOKEN` |
| `FINNHUB_API_KEY` | your Finnhub key | your `.env` line `FINNHUB_API_KEY` |
| `TELEGRAM_CHAT_ID` (optional) | your chat id | your `.env` line `TELEGRAM_CHAT_ID` — skips step 2 |

Save. Edge functions pick secrets up within a minute or two — no redeploy. You can confirm in the
SQL editor: `select id, status, sent_at, error from saa.outbox order by id;` — the M1 test message
flips from `pending` to `sent` on the next 2-minute poll.

What happens next on its own: within 2 minutes the `saa_telegram_poll` cron calls `telegram-send`,
which now has a token. The market-data cron starts filling `saa.calendar_days` at 7:15 AM ET and
`saa.price_ticks` every minute 9:28–16:02 ET on weekdays.

## 2. Say hello to the bot (1 min)

Open Telegram, find your bot by the username BotFather gave you, press **Start** (or type `/start`).
Within 2 minutes you get "Seeking Alpha Agent connected…" — that reply is the proof the whole
outbox → Telegram path works, and the queued M1 test message arrives right after it.

The system records your chat id itself (`saa.settings.telegram_chat_id`). Only that chat is ever
honored; anyone else messaging the bot is ignored.

Commands: `/status` · `/id` · `/halt` (sets the kill-switch flag; M2+ daemon flattens and stops) ·
`/resume` · `/help`.

## 3. TradingView: one indicator, one alert per symbol (5–10 min)

TradingView has no API for creating alerts, so this part is by hand, once per symbol.

1. Get the webhook secret: Supabase → **SQL Editor** → run
   `select value from saa.settings where key = 'tv_webhook_secret';` → copy the value.
   (Prefer your own? `select public.saa_set_setting('tv_webhook_secret', '<yours>');`)
2. TradingView → Pine Editor → paste `saa_fast_lane_triggers.pine` → **Add to chart**
   (1-minute chart; 5-minute also works). In the indicator's settings paste the secret into
   **Webhook secret**. Save the script so it appears under *My scripts*.
3. For each symbol on your list, open its 1-minute chart, add the indicator, then **Alert**:
   - Condition: **SAA Fast-Lane Triggers** → **Any alert() function call**
   - Expiration: Open-ended (Premium)
   - Notifications → **Webhook URL**:
     `https://zspbkcheounkwnpjkgrv.supabase.co/functions/v1/tv-webhook`
   - Message: leave the default — the script builds the JSON (symbol, lane, direction, price,
     bar time, RVOL, VWAP) and includes the secret.
   - Name it `SAA <SYMBOL>` and create.
4. Repeat per symbol. Starter set if you want one: SPY, QQQ, IWM + the liquid mega-caps you
   already watch. Every symbol that arrives on the webhook is in scope; single names get the
   default 2.5%/day vol assumption until the brief or the Friday review sets a better one.

Test it: on any symbol, create a throwaway alert with the same webhook URL and the message
`{"secret":"<your secret>","symbol":"ZZTEST","lane":"vwap","direction":"long","price":100}` and
fire it (TradingView's "test" isn't available for webhooks — use a condition that triggers now,
e.g. *SPY crossing* its current price). Then in the SQL editor:
`select * from saa.triggers order by id desc limit 3;` — a `ZZTEST` row means the path works.
Delete it with `delete from saa.triggers where symbol = 'ZZTEST';` (also
`delete from saa.shadow_trades where symbol = 'ZZTEST';` first if one opened).

Lanes and when they fire (all corpus-defined, all Tier 2 learnable in `saa.rules`):

| lane | fires when | opens a shadow trade? |
|---|---|---|
| `rvol` | time-of-day-normalized cumulative volume ≥ 2× its 20-session average (after 9:35, once/session) | yes, if ≤ 11:30 ET |
| `orb` | close breaks the 15-minute opening range with bar volume ≥ 1.5× average (until 10:30) | yes, if ≤ 10:30 ET |
| `vwap` | session-VWAP reclaim (long) / loss (short) after ≥ 3 bars on the other side; 30-min cooldown | yes, if ≤ 15:30 ET |
| `continuation` | at 10:30: close in the top/bottom fifth of the first-hour range, range ≥ 0.6 × daily ATR, RVOL ≥ 1.5 | yes, if ≤ 11:30 ET |
| `compression` | first bar of the day: 5-day range < 1.2 × ATR(60) and 20-day RV in the bottom quartile | no — flag for checklist item 2 |

Every shadow trade is a modeled 0DTE-style long option (0.5σ OTM, whole contract) scored on the
underlying's 1-minute path with a Black-Scholes price and the plan's trail rule; the M1 model is
directional evidence only — real option marks arrive with the broker feed in M2.

## 4. Monday 2026-09-28 — what you'll see

- 7:15 AM ET: calendar fetched (earnings, IPOs, VIX term structure) — silent.
- 7:40 AM ET: the brief in Telegram, ≤ 5 lines: regime read, catalyst windows, stand-down or
  the day's candidates with a probability each.
- 9:28 AM ET: opening snapshot — silent. 9:30–4:00: shadow trades open/close silently.
- 4:20 PM ET: the tally — shadow trades, R, hit rate to date, calibration, one journal line.
- Friday 4:45 PM ET: the distribution review draft (R histogram, expectancy, avg win/loss, top-3
  share, Brier by bucket, at most one proposed Tier 2 change — nothing changes until you reply).

If a run is missing: `/status` shows counts; the build log for this milestone lists where each
piece logs (`saa.run_log`).

## 5. M2 — the daemon on your Mac (10 minutes, then one command per trading day)

The daemon (`agent/daemon/`) is the process that will place the trades in M4+. In M2 it streams the
data plane and **places no orders**. It reads the same `.env`.

1. **Two more lines in `.env`** (already added as placeholders at the bottom of the file):
   `SUPABASE_URL=https://zspbkcheounkwnpjkgrv.supabase.co` (filled in) and `SUPABASE_SERVICE_ROLE_KEY=` → paste **one**
   key from Supabase → project **Quant edge** → **Project Settings** (gear, bottom left) → **API Keys**. The page shows
   two key families and either works here: the **secret key** (`sb_secret_…`, under *Publishable and secret API keys* —
   click *Create new API key* if none exists, name it `saa-daemon`) or the legacy **`service_role`** key (`eyJ…`, under
   *Legacy API keys*, click *Reveal*). Do **not** use `anon` or `sb_publishable_…` — those cannot write to `saa`. The
   key lets the daemon write bars, snapshots and heartbeats into the `saa` schema; never share it or paste it into chat.
   Save the file.
2. **Smoke test** (any day, ~30 s; first run builds a Python virtualenv, ~1 min):
   ```bash
   cd "$HOME/Desktop/Seeking Alpha Agent /agent/daemon"
   ./run.sh smoke
   ```
   Every line should say PASS (WARN on `dxlink` is normal when the market is closed). You get one
   short Telegram message from @SeekingABot — that is the outbox path working from the daemon.
   If Python 3.11+ is missing: `brew install python@3.12`, then run it again.
3. **Session run** (a weekday; start any time before 9:20 AM ET, leave the Mac awake, lid open or
   "Prevent automatic sleeping" on):
   ```bash
   cd "$HOME/Desktop/Seeking Alpha Agent /agent/daemon"
   ./run.sh session
   ```
   9:25 heartbeat and 4:20 EOD data report arrive on Telegram; the process exits at 4:25. Paste the
   EOD report into the next build chat — it is the M2 acceptance evidence (≥380 bars per index symbol,
   a snapshot per active symbol every 5 minutes, zero unhandled exceptions), and `saa.run_log`
   `daemon:session` carries the same numbers.
4. Optional: let launchd start it at 9:10 every weekday — instructions in the header of
   `agent/daemon/deploy/com.saa.daemon.plist`. It only helps while the Mac is awake; the VPS (M5) is
   what removes that dependency.

`./run.sh check` prints readiness by key name (never a value). Logs and the SQLite state live under
`agent/daemon/state/`. `agent/daemon/README.md` explains everything the daemon writes.

## 6. Still in `.env` (not needed until later)

`ANTHROPIC_API_KEY` (M3 — daemon judgment calls). `TT_PROD_*` is used from M2 on for **market data
only** (read-only); live orders on the production account are M5.
