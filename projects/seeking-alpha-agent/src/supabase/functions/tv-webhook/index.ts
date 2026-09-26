// tv-webhook — receives TradingView alert webhooks and files them as saa.triggers
// (opening a modeled shadow trade when the lane allows it).
//
// Auth: TradingView cannot send headers, so the alert message body must be JSON that
// carries `secret` = saa.settings.tv_webhook_secret. Optionally the source IP is checked
// against TradingView's published webhook IPs (settings.tv_ip_check / tv_ip_allowlist).
// Internal callers (pg_net tests) may instead send the x-saa-key header, which bypasses
// the IP check but still requires the body secret.
//
// Deployed with verify_jwt = false (custom auth above).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { getSetting, json, logRun, rpc, safeEqual } from "./saa.ts";

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return json({ ok: false, error: "POST only" }, 405);

  const raw = await req.text();
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(raw);
  } catch {
    await logRun("tv-webhook", false, { error: "body is not JSON", sample: raw.slice(0, 200) });
    return json({ ok: false, error: "body must be JSON" }, 400);
  }

  const expectedSecret = await getSetting("tv_webhook_secret");
  const secret = typeof payload.secret === "string" ? payload.secret : "";
  if (!safeEqual(secret, expectedSecret)) {
    await logRun("tv-webhook", false, { error: "bad secret", symbol: payload.symbol ?? null });
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  const xff = req.headers.get("x-forwarded-for") ?? req.headers.get("cf-connecting-ip") ?? "";
  const sourceIp = xff.split(",")[0].trim() || null;
  const internal = safeEqual(req.headers.get("x-saa-key"), await getSetting("internal_key"));
  if (!internal && (await getSetting("tv_ip_check")) === "true" && sourceIp) {
    const allow = ((await getSetting("tv_ip_allowlist")) ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    if (allow.length && !allow.includes(sourceIp)) {
      await logRun("tv-webhook", false, { error: "ip not allowed", ip: sourceIp });
      return json({ ok: false, error: "forbidden" }, 403);
    }
  }

  const { secret: _drop, ...clean } = payload;
  try {
    const result = await rpc("saa_ingest_trigger", { p_payload: clean, p_source_ip: sourceIp });
    await logRun("tv-webhook", true, { ...result, symbol: clean.symbol ?? null, lane: clean.lane ?? null, ip: sourceIp, internal });
    return json(result, 200);
  } catch (e) {
    await logRun("tv-webhook", false, { error: String(e), payload: clean });
    return json({ ok: false, error: String(e) }, 500);
  }
});
