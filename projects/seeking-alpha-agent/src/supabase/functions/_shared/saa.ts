// Shared helpers for the Seeking Alpha Agent edge functions.
// Deployed as `saa.ts` inside each function (the MCP deploy uploads per-function file sets).
import { createClient } from "npm:@supabase/supabase-js@2";

export const sb = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false, autoRefreshToken: false } },
);

// deno-lint-ignore no-explicit-any
export async function rpc<T = any>(fn: string, args: Record<string, unknown> = {}): Promise<T> {
  const { data, error } = await sb.rpc(fn, args);
  if (error) throw new Error(`${fn}: ${error.message}`);
  return data as T;
}

export const getSetting = (key: string) => rpc<string | null>("saa_get_setting", { p_key: key });
export const setSetting = (key: string, value: string) => rpc<null>("saa_set_setting", { p_key: key, p_value: value });

export async function logRun(job: string, ok: boolean, detail: unknown = null) {
  try {
    await rpc("saa_log_run", { p_job: job, p_ok: ok, p_detail: detail });
  } catch (e) {
    console.error("logRun failed", job, String(e));
  }
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Constant-time string comparison (both sides hex/ascii). */
export function safeEqual(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Reject unless the request carries the internal key held in saa.settings. Returns a Response on failure. */
export async function requireInternalKey(req: Request): Promise<Response | null> {
  const expected = await getSetting("internal_key");
  if (!safeEqual(req.headers.get("x-saa-key"), expected)) return json({ ok: false, error: "unauthorized" }, 401);
  return null;
}

export interface EtNow {
  date: string; // YYYY-MM-DD in America/New_York
  hh: number;
  mm: number;
  minutes: number; // minutes since midnight ET
  dow: number; // 0=Sun .. 6=Sat
  weekday: boolean;
  hhmm: string;
}

/** Current wall-clock time in America/New_York, DST-safe. */
export function etNow(d: Date = new Date()): EtNow {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    weekday: "short",
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const hh = Number(get("hour")) % 24;
  const mm = Number(get("minute"));
  const dowMap: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  const dow = dowMap[get("weekday")] ?? 0;
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    hh,
    mm,
    minutes: hh * 60 + mm,
    dow,
    weekday: dow >= 1 && dow <= 5,
    hhmm: `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`,
  };
}

/** "09:28-16:02" → [568, 962] minutes. */
export function parseWindow(s: string | null | undefined, fallback: [number, number]): [number, number] {
  const m = /^(\d{2}):(\d{2})-(\d{2}):(\d{2})$/.exec(s ?? "");
  if (!m) return fallback;
  return [Number(m[1]) * 60 + Number(m[2]), Number(m[3]) * 60 + Number(m[4])];
}

/** ET date arithmetic: previous weekday as YYYY-MM-DD. */
export function previousWeekday(isoDate: string): string {
  const d = new Date(isoDate + "T12:00:00Z");
  do {
    d.setUTCDate(d.getUTCDate() - 1);
  } while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
  return d.toISOString().slice(0, 10);
}

export async function fetchJson(url: string, init: RequestInit = {}, timeoutMs = 12000): Promise<unknown> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...init, signal: ctrl.signal });
    const text = await r.text();
    if (!r.ok) throw new Error(`HTTP ${r.status} ${text.slice(0, 200)}`);
    return text ? JSON.parse(text) : null;
  } finally {
    clearTimeout(t);
  }
}
