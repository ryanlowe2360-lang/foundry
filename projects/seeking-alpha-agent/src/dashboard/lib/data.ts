import { readFile } from "node:fs/promises";
import path from "node:path";
import type { Dashboard } from "./types";

/** Server-only. Reads the one RPC the dashboard needs with the service-role key — the key never reaches the browser.
 *  `SAA_DASHBOARD_FIXTURE=<path.json>` renders a saved document instead (local runs, Playwright screenshots, tests). */
export async function getDashboard(days = 90): Promise<{ doc: Dashboard; source: string }> {
  const fixture = process.env.SAA_DASHBOARD_FIXTURE;
  if (fixture) {
    const p = path.isAbsolute(fixture) ? fixture : path.join(process.cwd(), fixture);
    const doc = JSON.parse(await readFile(p, "utf8")) as Dashboard;
    return { doc, source: `fixture ${path.basename(p)}` };
  }
  const url = (process.env.SUPABASE_URL || "").replace(/\/+$/, "");
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SECRET_KEY || "";
  if (!url || !key) throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set (or set SAA_DASHBOARD_FIXTURE)");
  const headers: Record<string, string> = { apikey: key, "Content-Type": "application/json", Accept: "application/json" };
  if (!key.startsWith("sb_")) headers.Authorization = `Bearer ${key}`; // legacy service_role JWT; new secret keys go on apikey only
  const r = await fetch(`${url}/rest/v1/rpc/saa_dashboard`, { method: "POST", headers, body: JSON.stringify({ p_days: days }), cache: "no-store" });
  if (!r.ok) throw new Error(`saa_dashboard HTTP ${r.status}`); // never echo the body: it could carry the key on a 401
  const doc = (await r.json()) as Dashboard;
  return { doc, source: "supabase" };
}
