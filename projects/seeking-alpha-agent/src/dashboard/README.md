# Seeking Alpha Agent — dashboard (M4, read-only)

A one-page, server-rendered Next.js 15 app: ledger (engine shadows at real marks + M1 modeled shadows, with their
paper execution), R histogram, expectancy by source, posterior p / W → Kelly size, Brier by bucket, paper execution
panel (proposals, reconciliations, recent trades), required-vs-realized growth toward $5,000,000 by 2027-09-25, the
daemon's last run and today's engine decisions. Nothing is editable from the page; nothing in the browser can read
Supabase — the page is rendered on the server with the service-role key and reads **one** RPC, `public.saa_dashboard`.

```
app/page.tsx         the page (server component, dynamic — every request re-reads the RPC)
app/api/health       {ok, mode} — liveness only
middleware.ts        access gate: DASHBOARD_ACCESS_KEY (?key=… once, then a cookie)
lib/data.ts          the RPC call (or a saved document via SAA_DASHBOARD_FIXTURE)
lib/posterior.ts     port of saa_daemon/engine/kelly.py (same numbers as ./run.sh kelly-table)
lib/growth.ts        NYSE trading-day calendar 2026–27 → the required curve
components/charts    pure SVG charts (histogram, growth lines, calibration dumbbells) with table views
fixtures/            live-2026-10-02.json = a real saa_dashboard document; demo-paper.json = synthetic paper rows (rendering only)
scripts/screenshot.mjs  Playwright: builds nothing, starts next start, screenshots light + dark
```

## Run locally

```bash
cd src/dashboard
npm install
cp .env.example .env.local     # fill SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (same key as the daemon's .env) + DASHBOARD_ACCESS_KEY
npm run dev                    # http://localhost:3000/?key=<DASHBOARD_ACCESS_KEY>
# or render a saved document with no network:
SAA_DASHBOARD_FIXTURE=fixtures/live-2026-10-02.json npm run dev
```

Screenshots (evidence): `npm run build && node scripts/screenshot.mjs --fixture fixtures/live-2026-10-02.json --out ../../notes/screenshots --name dashboard-live`
(uses `/opt/pw-browsers/chromium` when present, else `PW_CHROMIUM_PATH`, else Playwright's own download).

## Deploy to Vercel (Ryan, ~5 minutes, no CLI needed)

`vercel.json` pins the framework and an install command that skips Playwright's browser download (`npm ci` with
`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`), so the Git import works as is.

1. vercel.com → **Add New… → Project** → import `ryanlowe2360-lang/foundry` (authorize the Vercel GitHub app for that
   repo if asked).
2. **Root Directory** → `projects/seeking-alpha-agent/src/dashboard` (framework Next.js is detected). Name the project
   `saa-dashboard`.
3. **Environment Variables** (Production + Preview), **all server-side, no `NEXT_PUBLIC_`** — type them into the Vercel
   form, never into a chat:
   - `SUPABASE_URL` = `https://zspbkcheounkwnpjkgrv.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = the same key the daemon uses — copy it out of `.env` in `Desktop/Seeking Alpha Agent /`
   - `DASHBOARD_ACCESS_KEY` = a long random string (`openssl rand -hex 24` in Terminal)
4. **Deploy**. Open `https://<project>.vercel.app/?key=<DASHBOARD_ACCESS_KEY>` once; the cookie keeps you in for 30 days.
   Without the key the page answers 401 — that is the lock working.
5. Tell Claude the URL (not the key); it goes into `STATE.json` (`deploy_url`) via
   `python3 scripts/foundry.py touch seeking-alpha-agent --deploy-url …`. Every push to `main` redeploys.

CLI alternative: `npm i -g vercel`, then from `src/dashboard/`: `vercel link` → `vercel env add …` ×3 → `vercel --prod`.
Vercel's own "Deployment Protection" can be switched on as a second lock if wanted; the access key is the first one.

## What the numbers mean (accountant's version)

- **R** = result ÷ premium paid. −1R is a full loss; +0.42R on a $114 contract is +$48.
- **Posterior p / W**: the measured hit rate and average win, mixed with the corpus prior (30 % / 5R, worth 30 trades)
  and shrunk toward the breakeven hit rate 1/(1+W). With no closed gate-fired trades, the usable edge is ε = 0.5 %
  → full Kelly 0.6 % → the one-contract floor. The card shows exactly what the daemon sizes with.
- **Required path**: $1,000 → $5,000,000 is a ×5,000; spread over the NYSE trading days to 2027-09-25 that is
  ≈3.47 % per trading day (5,000^(1/250) − 1). The realized line is the paper ledger's cumulative realized P&L.
- **Brier by bucket**: the brief's probabilities grouped by bucket against how often those trades actually won;
  Brier = mean (p − outcome)². Lower is better; 0.25 is a coin flip.
