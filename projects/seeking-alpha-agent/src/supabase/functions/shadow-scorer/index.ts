// shadow-scorer — opens the brief's checklist shadows once their window starts and closes every
// shadow trade whose time stop, trail stop or expiry has been hit, using the modeled option
// price over saa.price_ticks (all logic lives in SQL: saa.score_shadow_trade et al.).
// Driven by pg_cron every 5 minutes during the session. Auth: x-saa-key. verify_jwt = false.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { etNow, json, logRun, requireInternalKey, rpc } from "./saa.ts";

Deno.serve(async (req: Request) => {
  const denied = await requireInternalKey(req);
  if (denied) return denied;
  let force = false;
  try {
    const b = await req.json();
    force = b?.force === true;
  } catch { /* no body */ }
  const now = etNow();
  // The session gate is wide on purpose: a trade left open by a feed gap still gets voided the next morning.
  if (!force && (!now.weekday || now.minutes < 9 * 60 + 30 || now.minutes > 16 * 60 + 30)) {
    return json({ ok: true, skipped: `outside gate (ET ${now.hhmm})` });
  }
  try {
    const result = await rpc<Record<string, number>>("saa_score_shadow_trades");
    const touched = (result.closed ?? 0) + (result.voided ?? 0) + (result.checklist_shadows_opened ?? 0) + (result.two_sided_resolved ?? 0);
    if (touched > 0 || now.mm % 30 === 0) await logRun("shadow-scorer", true, result);
    return json({ ok: true, ...result });
  } catch (e) {
    await logRun("shadow-scorer", false, { error: String(e) });
    return json({ ok: false, error: String(e) }, 500);
  }
});
