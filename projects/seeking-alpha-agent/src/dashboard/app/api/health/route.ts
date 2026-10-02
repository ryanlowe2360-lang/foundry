import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/** Liveness only — no data, no secrets. */
export function GET() {
  return NextResponse.json({ ok: true, app: "saa-dashboard", mode: process.env.SAA_DASHBOARD_FIXTURE ? "fixture" : "supabase", at: new Date().toISOString() });
}
