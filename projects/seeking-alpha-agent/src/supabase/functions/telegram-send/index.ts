// telegram-send — flushes saa.outbox to Ryan's Telegram DM and handles inbound commands.
//
// Invoked by: the saa.outbox insert trigger (reason=outbox_insert) and a pg_cron poll every
// 2 minutes (reason=poll). Never accepts free text from the caller: it only sends rows that
// already exist in saa.outbox, so the endpoint cannot be used to spam the chat.
//
// Secrets: TELEGRAM_BOT_TOKEN (Supabase edge-function secret, set by Ryan in the dashboard).
// The chat id is captured automatically from the first private message (/start) and stored in
// saa.settings.telegram_chat_id. Commands: /start /id /status /halt /resume /help.
//
// Deployed with verify_jwt = false; custom auth = x-saa-key header (saa.settings.internal_key).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { fetchJson, getSetting, json, logRun, requireInternalKey, rpc, setSetting } from "./saa.ts";

const MAX_LEN = 4000;

interface TgUpdate {
  update_id: number;
  message?: { message_id: number; text?: string; chat: { id: number; type: string; first_name?: string; username?: string } };
}

async function tg(token: string, method: string, body: Record<string, unknown>) {
  return await fetchJson(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 15000) as { ok: boolean; result?: unknown; description?: string };
}

async function send(token: string, chatId: string, text: string, parseMode?: string | null) {
  const chunks: string[] = [];
  for (let i = 0; i < text.length; i += MAX_LEN) chunks.push(text.slice(i, i + MAX_LEN));
  let lastId: number | null = null;
  for (const chunk of chunks) {
    const body: Record<string, unknown> = { chat_id: chatId, text: chunk, disable_web_page_preview: true };
    if (parseMode) body.parse_mode = parseMode;
    const r = await tg(token, "sendMessage", body);
    if (!r.ok) throw new Error(r.description ?? "sendMessage failed");
    lastId = (r.result as { message_id: number }).message_id;
  }
  return lastId;
}

async function handleCommand(token: string, chatId: string, text: string) {
  const cmd = text.trim().split(/\s+/)[0].toLowerCase().replace(/@.*$/, "");
  switch (cmd) {
    case "/start":
      await send(token, chatId, "Seeking Alpha Agent connected. This chat will receive the 7:40 AM brief, event alerts, the 4:20 PM tally and the Friday review.\nCommands: /status /id /halt /resume /help");
      break;
    case "/id":
      await send(token, chatId, `chat id: ${chatId}`);
      break;
    case "/status": {
      const s = await rpc<string>("saa_status_text");
      const halt = (await getSetting("halt")) === "true";
      await send(token, chatId, s + (halt ? "\nHALT flag is set." : ""));
      break;
    }
    case "/halt":
      await setSetting("halt", "true");
      await send(token, chatId, "HALT flag set. M1 places no orders; from M2 the daemon flattens and stops when this flag is set. /resume clears it.");
      break;
    case "/resume":
      await setSetting("halt", "false");
      await send(token, chatId, "HALT flag cleared.");
      break;
    case "/help":
      await send(token, chatId, "Commands: /status — today's counts and the ledger to date · /id — this chat's id · /halt — set the kill-switch flag · /resume — clear it");
      break;
    default:
      // ignore free text; the agent is not a chatbot
      break;
  }
}

Deno.serve(async (req: Request) => {
  const denied = await requireInternalKey(req);
  if (denied) return denied;
  let reason = "manual";
  try {
    const b = await req.json();
    if (b && typeof b.reason === "string") reason = b.reason;
  } catch { /* empty body is fine */ }

  const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
  if (!token) {
    if (reason !== "poll") await logRun("telegram-send", false, { reason, error: "TELEGRAM_BOT_TOKEN secret not set; outbox left pending" });
    return json({ ok: false, error: "TELEGRAM_BOT_TOKEN not set" }, 200);
  }

  const detail: Record<string, unknown> = { reason };
  let chatId = (await getSetting("telegram_chat_id")) ?? "";

  // 1) inbound updates: capture the chat id, answer commands
  try {
    const offset = Number((await getSetting("telegram_update_offset")) ?? "0") || 0;
    const r = await tg(token, "getUpdates", { offset, timeout: 0, allowed_updates: ["message"] });
    const updates = (r.ok ? (r.result as TgUpdate[]) : []) ?? [];
    let maxId = offset - 1;
    for (const u of updates) {
      maxId = Math.max(maxId, u.update_id);
      const m = u.message;
      if (!m || m.chat.type !== "private" || !m.text) continue;
      const from = String(m.chat.id);
      if (!chatId) {
        chatId = from;
        await setSetting("telegram_chat_id", chatId);
        detail.chat_id_captured = true;
        if (!m.text.startsWith("/start")) await handleCommand(token, chatId, "/start");
      }
      if (from !== chatId) continue; // only the owner's chat is honored
      if (m.text.startsWith("/")) await handleCommand(token, chatId, m.text);
    }
    if (updates.length) await setSetting("telegram_update_offset", String(maxId + 1));
    detail.updates = updates.length;
  } catch (e) {
    detail.poll_error = String(e);
  }

  // 2) flush the outbox
  let sent = 0, failed = 0;
  if (chatId) {
    try {
      const pending = await rpc<{ id: number; kind: string; body: string; parse_mode: string | null; attempts: number }[]>("saa_outbox_pending", { p_limit: 20 });
      for (const row of pending ?? []) {
        try {
          const msgId = await send(token, chatId, row.body, row.parse_mode);
          await rpc("saa_outbox_mark", { p_id: row.id, p_ok: true, p_error: null, p_msg_id: msgId });
          sent++;
        } catch (e) {
          const err = String(e);
          await rpc("saa_outbox_mark", { p_id: row.id, p_ok: false, p_error: err.slice(0, 500), p_msg_id: null });
          failed++;
          if (/Too Many Requests|429/.test(err)) break;
        }
      }
    } catch (e) {
      detail.flush_error = String(e);
    }
  } else {
    detail.note = "no chat id yet — send /start to the bot";
  }
  detail.sent = sent;
  detail.failed = failed;
  const ok = !detail.flush_error && failed === 0;
  if (reason !== "poll" || sent || failed || detail.chat_id_captured || detail.poll_error) await logRun("telegram-send", ok, detail);
  return json({ ok, ...detail });
});
