// market-data — the M1 data plane, driven by pg_cron (all outbound calls live here because the
// build/scheduled-task environment cannot reach these hosts).
//
//   mode=calendar      07:15 ET weekdays: Finnhub earnings (today BMO/AMC + yesterday AMC), IPOs,
//                      economic calendar (premium — recorded as an error on the free tier),
//                      Cboe VIX term structure (VIX, VIX1D, VIX9D, VIX3M), index quotes
//                      → saa.calendar_days + a 'vix_term' snapshot.
//   mode=quotes        every minute 09:28–16:02 ET: Finnhub /quote for the active symbol set
//                      (index set ∪ open shadow trades ∪ today's triggers ∪ the brief's watch list)
//                      → saa.price_ticks (1-min resolution).
//   mode=open_snapshot 09:28 ET: VIX term + index quotes → 'open_snapshot'.
//
// Body: { mode, force?: boolean, date?: 'YYYY-MM-DD' }. `force` skips the ET time gates (tests).
// Secrets: FINNHUB_API_KEY (edge-function secret). Auth: x-saa-key. verify_jwt = false.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { etNow, fetchJson, getSetting, json, logRun, parseWindow, previousWeekday, requireInternalKey, rpc } from "./saa.ts";

const FINNHUB = "https://finnhub.io/api/v1";
const CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes";

type Quote = { c: number; d: number; dp: number; h: number; l: number; o: number; pc: number; t: number };

async function finnhub(path: string, key: string): Promise<unknown> {
  const sep = path.includes("?") ? "&" : "?";
  return await fetchJson(`${FINNHUB}${path}${sep}token=${encodeURIComponent(key)}`);
}

async function cboe(sym: string): Promise<number | null> {
  const r = await fetchJson(`${CBOE}/${sym}.json`) as { data?: { current_price?: number; close?: number; prev_day_close?: number } };
  const v = r?.data?.current_price ?? r?.data?.close ?? r?.data?.prev_day_close ?? null;
  return typeof v === "number" && v > 0 ? v : null;
}

async function quotes(symbols: string[], key: string, concurrency = 8): Promise<{ ok: Record<string, Quote>; errors: string[] }> {
  const ok: Record<string, Quote> = {};
  const errors: string[] = [];
  const queue = [...symbols];
  async function worker() {
    while (queue.length) {
      const s = queue.shift()!;
      try {
        const q = await finnhub(`/quote?symbol=${encodeURIComponent(s)}`, key) as Quote;
        if (q && typeof q.c === "number" && q.c > 0) ok[s] = q;
        else errors.push(`${s}: empty quote`);
      } catch (e) {
        errors.push(`${s}: ${String(e).slice(0, 120)}`);
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, symbols.length) }, worker));
  return { ok, errors };
}

async function vixTerm(): Promise<{ vix: Record<string, number | null>; errors: string[] }> {
  const errors: string[] = [];
  const out: Record<string, number | null> = {};
  for (const [k, sym] of [["vix", "_VIX"], ["vix1d", "_VIX1D"], ["vix9d", "_VIX9D"], ["vix3m", "_VIX3M"]] as const) {
    try {
      out[k] = await cboe(sym);
    } catch (e) {
      out[k] = null;
      errors.push(`cboe ${sym}: ${String(e).slice(0, 120)}`);
    }
  }
  out.fetched_at = Date.now();
  return { vix: out, errors };
}

Deno.serve(async (req: Request) => {
  const denied = await requireInternalKey(req);
  if (denied) return denied;
  let body: { mode?: string; force?: boolean; date?: string } = {};
  try { body = await req.json(); } catch { /* defaults */ }
  const mode = body.mode ?? "quotes";
  const force = body.force === true;
  const now = etNow();
  // time gates first (cron fires in both EDT and EST slots; only the right one does work)
  if (!force) {
    let skip: string | null = null;
    if (mode === "calendar" && (!now.weekday || now.hh !== 7)) skip = `outside gate (ET ${now.hhmm}, dow ${now.dow})`;
    if (mode === "open_snapshot" && (!now.weekday || now.minutes < 9 * 60 + 20 || now.minutes > 9 * 60 + 35)) skip = `outside gate (ET ${now.hhmm})`;
    if (mode === "quotes") {
      const [w0, w1] = parseWindow(await getSetting("quotes_window_et"), [9 * 60 + 28, 16 * 60 + 2]);
      if (!now.weekday || now.minutes < w0 || now.minutes > w1) skip = `outside gate (ET ${now.hhmm})`;
    }
    if (skip) return json({ ok: true, skipped: skip });
  }
  const key = Deno.env.get("FINNHUB_API_KEY") ?? "";
  if (!key) {
    if (mode !== "quotes" || now.mm % 30 === 0) await logRun(`market-data:${mode}`, false, { error: "FINNHUB_API_KEY secret not set" });
    return json({ ok: false, error: "FINNHUB_API_KEY not set" });
  }
  const indexSymbols = ((await getSetting("index_symbols")) ?? "SPY,QQQ,IWM").split(",").map((s) => s.trim()).filter(Boolean);

  if (mode === "calendar") {
    const date = body.date ?? now.date;
    const prev = previousWeekday(date);
    const errors: string[] = [];
    const earnings: unknown[] = [];
    for (const [d, tag] of [[date, "today"], [prev, "prev"]] as const) {
      try {
        const r = await finnhub(`/calendar/earnings?from=${d}&to=${d}`, key) as { earningsCalendar?: Record<string, unknown>[] };
        for (const e of r?.earningsCalendar ?? []) {
          const hour = String(e.hour ?? "");
          // today's catalysts: today BMO/DMH, or yesterday AMC (gaps into today's open)
          if (tag === "prev" && hour !== "amc") continue;
          earnings.push({ symbol: e.symbol, date: e.date, hour, when: tag === "prev" ? "yesterday_amc" : `today_${hour || "unknown"}`,
            eps_estimate: e.epsEstimate ?? null, revenue_estimate: e.revenueEstimate ?? null, quarter: e.quarter ?? null, year: e.year ?? null });
        }
      } catch (e) { errors.push(`earnings ${d}: ${String(e).slice(0, 160)}`); }
    }
    let ipos: unknown[] = [];
    try {
      const r = await finnhub(`/calendar/ipo?from=${date}&to=${date}`, key) as { ipoCalendar?: unknown[] };
      ipos = r?.ipoCalendar ?? [];
    } catch (e) { errors.push(`ipo: ${String(e).slice(0, 160)}`); }
    let econ: unknown[] = [];
    try {
      const r = await finnhub(`/calendar/economic?from=${date}&to=${date}`, key) as { economicCalendar?: Record<string, unknown>[] };
      econ = (r?.economicCalendar ?? []).filter((e) => String(e.country ?? "").toUpperCase() === "US");
    } catch (e) { errors.push(`econ (premium on free tier): ${String(e).slice(0, 120)}`); }
    const { vix, errors: vixErr } = await vixTerm();
    errors.push(...vixErr);
    const { ok: iq, errors: qErr } = await quotes(indexSymbols, key);
    errors.push(...qErr);
    const payload = { earnings: earnings.slice(0, 600), ipos, econ, vix, index_quotes: iq, errors };
    try {
      await rpc("saa_upsert_calendar", { p_date: date, p_payload: payload });
      await rpc("saa_snapshot", { p_kind: "vix_term", p_payload: { date, ...vix } });
      await logRun("market-data:calendar", errors.length === 0, { date, earnings: earnings.length, ipos: ipos.length, econ: econ.length, vix, errors });
      return json({ ok: true, date, earnings: earnings.length, ipos: ipos.length, econ: econ.length, vix, errors });
    } catch (e) {
      await logRun("market-data:calendar", false, { date, error: String(e), errors });
      return json({ ok: false, error: String(e) }, 500);
    }
  }

  if (mode === "open_snapshot") {
    const { vix, errors } = await vixTerm();
    const { ok: iq, errors: qErr } = await quotes(indexSymbols, key);
    errors.push(...qErr);
    await rpc("saa_snapshot", { p_kind: "open_snapshot", p_payload: { date: now.date, et: now.hhmm, vix, index_quotes: iq } });
    await logRun("market-data:open_snapshot", errors.length === 0, { vix, errors });
    return json({ ok: true, vix, errors });
  }

  // mode = quotes
  const symbols = (await rpc<string[] | null>("saa_active_symbols")) ?? indexSymbols;
  const { ok: qs, errors } = await quotes(symbols, key);
  const minute = new Date(Math.floor(Date.now() / 60000) * 60000).toISOString();
  const rows = Object.entries(qs).map(([symbol, q]) => ({ symbol, ts: minute, price: q.c, day_open: q.o, day_high: q.h, day_low: q.l, prev_close: q.pc }));
  let inserted = 0;
  try {
    inserted = await rpc<number>("saa_insert_ticks", { p_rows: rows });
  } catch (e) {
    errors.push(`insert: ${String(e)}`);
  }
  if (errors.length || now.mm % 30 === 0) await logRun("market-data:quotes", errors.length === 0, { symbols: symbols.length, inserted, errors });
  return json({ ok: errors.length === 0, symbols: symbols.length, inserted, errors });
});
