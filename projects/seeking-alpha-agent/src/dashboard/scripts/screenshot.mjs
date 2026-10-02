// Playwright screenshot of the dashboard: builds nothing, starts `next start` on a free port with the given fixture
// (or the real Supabase env), opens the page through the access-key gate, saves light + dark full-page PNGs.
//   node scripts/screenshot.mjs [--fixture fixtures/live-2026-10-02.json] [--out ../../notes/screenshots] [--name dashboard]
// Needs `npm run build` first (production server). Chromium comes from the `playwright` package (preinstalled in the cloud build box).
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { chromium } from "playwright";

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const fixture = opt("fixture", process.env.SAA_DASHBOARD_FIXTURE || "");
const outDir = path.resolve(opt("out", "screenshots"));
const name = opt("name", "dashboard");
const key = process.env.DASHBOARD_ACCESS_KEY || "screenshot-key";

const port = await new Promise((resolve) => {
  const srv = net.createServer();
  srv.listen(0, () => { const p = srv.address().port; srv.close(() => resolve(p)); });
});
const env = { ...process.env, PORT: String(port), DASHBOARD_ACCESS_KEY: key, NODE_ENV: "production" };
if (fixture) env.SAA_DASHBOARD_FIXTURE = fixture;
const server = spawn("node", ["node_modules/next/dist/bin/next", "start", "-p", String(port)], { env, stdio: ["ignore", "pipe", "pipe"] });
server.stdout.on("data", (d) => process.stdout.write(`[next] ${d}`));
server.stderr.on("data", (d) => process.stderr.write(`[next] ${d}`));

const base = `http://127.0.0.1:${port}`;
for (let i = 0; i < 60; i++) {
  try {
    const r = await fetch(`${base}/api/health`);
    if (r.ok) break;
  } catch { /* not up yet */ }
  await new Promise((r) => setTimeout(r, 500));
}
await mkdir(outDir, { recursive: true });
// A preinstalled Chromium (PW_CHROMIUM_PATH or the cloud build box's /opt/pw-browsers/chromium) avoids a download.
import { existsSync } from "node:fs";
const exe = process.env.PW_CHROMIUM_PATH || (existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined);
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
try {
  // the gate: a wrong key is refused, the right key sets the cookie
  const probe = await browser.newPage();
  const bad = await probe.goto(`${base}/`);
  console.log(`gate without key → HTTP ${bad.status()}`);
  await probe.close();
  for (const scheme of ["light", "dark"]) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const res = await page.goto(`${base}/?key=${encodeURIComponent(key)}`, { waitUntil: "networkidle" });
    console.log(`${scheme}: HTTP ${res.status()} ${page.url()}`);
    const title = await page.textContent("h1");
    if (!title || !title.includes("ledger")) throw new Error(`unexpected page: ${title}`);
    const file = path.join(outDir, `${name}-${scheme}.png`);
    await page.screenshot({ path: file, fullPage: true });
    const cards = await page.locator("section.card").count();
    const rows = await page.locator("table tbody tr").count();
    console.log(`saved ${file} · ${cards} cards · ${rows} table rows · h1 "${title}"`);
    await ctx.close();
  }
} finally {
  await browser.close();
  server.kill("SIGTERM");
}
