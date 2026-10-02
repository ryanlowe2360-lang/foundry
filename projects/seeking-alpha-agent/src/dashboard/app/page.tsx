import { Calibration, GrowthChart, Legend, RBadge, RHistogram } from "@/components/charts";
import { getDashboard } from "@/lib/data";
import { et, money, num, pct, r, signedMoney, todayEt } from "@/lib/format";
import { growthSeries } from "@/lib/growth";
import { expectancy, growthPerTrade, posterior, tradesToMultiple } from "@/lib/posterior";
import type { Dashboard, LedgerRow } from "@/lib/types";

export const dynamic = "force-dynamic";

function streak(records: Dashboard["daily_records"]): number {
  let n = 0;
  for (const d of records) {
    if (d.complete) n += 1;
    else break;
  }
  return n;
}

function sourceLabel(row: LedgerRow): string {
  if (row.source === "engine") return row.engine_source === "gate" ? "engine · gate" : `engine · ${row.lane ?? "fast lane"}`;
  return `${row.source ?? "?"} (modeled)`;
}

export default async function Page() {
  let data: { doc: Dashboard; source: string };
  try {
    data = await getDashboard(90);
  } catch (e) {
    return (
      <main className="page">
        <div className="masthead"><h1>SAA ledger</h1></div>
        <div className="card error"><h2>Data unavailable</h2><p className="hint">{(e as Error).message}</p></div>
      </main>
    );
  }
  const { doc, source } = data;
  const today = todayEt(new Date(doc.generated_at));          // the document's own clock, so a saved document renders the same later
  const account = Number(doc.settings?.account_size ?? 1000) || 1000;
  const k = Number(doc.settings?.kelly_k ?? 0.5) || 0.5;
  const post = posterior(doc.r_values.gate);
  const gPerTrade = growthPerTrade(post.p, post.w, post.f_full);
  const gHalf = growthPerTrade(post.p, post.w, post.f_full * k);
  const toTargetFull = tradesToMultiple(gPerTrade, doc.goal.target / doc.goal.start_equity);
  const toTargetK = tradesToMultiple(gHalf, doc.goal.target / doc.goal.start_equity);
  const shadowByDate = new Map<string, number>();
  for (const row of doc.ledger) {
    if (row.source === "engine" && row.engine_source === "gate" && row.status === "closed" && row.shadow_pnl !== null)
      shadowByDate.set(row.trade_date, (shadowByDate.get(row.trade_date) ?? 0) + row.shadow_pnl);
  }
  const growth = growthSeries(doc.goal, today, doc.paper.equity_curve, shadowByDate);
  const gap = growth.realizedToday - growth.requiredToday;
  const gateStats = doc.stats.find((s) => s.source === "engine" && s.engine_source === "gate");
  const paperExp = expectancy(doc.r_values.paper);
  const fastExp = expectancy(doc.r_values.fast_lane);
  const closedGate = doc.r_values.gate.length;
  const halted = (doc.settings?.halt ?? "").toLowerCase() === "true";
  const daemon = doc.daemon;
  const feedMode = daemon?.feed?.mode ?? "?";
  const lastSeenMin = daemon?.last_seen ? Math.max(0, Math.round((new Date(doc.generated_at).getTime() - new Date(daemon.last_seen).getTime()) / 60000)) : null;
  const recStreak = streak(doc.daily_records);
  const appr = doc.paper.approvals || {};
  const apprTotal = Object.values(appr).reduce((a, b) => a + b, 0);
  const recon = doc.paper.reconciliations;

  return (
    <main className="page">
      <div className="masthead">
        <div>
          <h1>Seeking Alpha Agent — ledger</h1>
          <div className="sub">
            {doc.goal.start_equity.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })} → {money(doc.goal.target)} by {doc.goal.target_date} · read-only ·
            generated {et(doc.generated_at, true)} ET · {source}
          </div>
        </div>
        <div>
          {halted ? <span className="badge serious">HALTED</span> : <span className="badge good">kill switch armed</span>}{" "}
          <span className={`badge ${feedMode === "realtime" ? "good" : feedMode === "DELAYED" ? "warn" : ""}`}>feed {feedMode}{daemon?.feed?.lag_s != null ? ` ${Math.round(daemon.feed.lag_s)}s` : ""}</span>{" "}
          <span className="badge">daemon v{daemon?.version ?? "?"} · {daemon?.status ?? "no run"}{lastSeenMin !== null ? ` · seen ${lastSeenMin} min ago` : ""}</span>
        </div>
      </div>

      <div className="grid">
        <section className="card">
          <div className="hero">
            <div>
              <div className="label">Equity today — paper ledger (start + realized P&amp;L)</div>
              <div className="figure">{money(growth.realizedToday)}</div>
              <div className="delta">
                required today {money(growth.requiredToday)} ·{" "}
                <span className={gap >= 0 ? "good" : "serious"}>{gap >= 0 ? "+" : "−"}{money(Math.abs(gap))} vs plan</span>
                {" "}· day {growth.elapsedTradingDays} of {growth.totalTradingDays} · {pct(growth.perDayRequired, 2)} per trading day required
              </div>
            </div>
            <div className="tiles hero-tiles">
              <div className="tile"><div className="label">Paper trades (closed)</div><div className="value">{doc.paper.closed}</div><div className="delta">{doc.paper.open} open · {doc.paper.trades} in window</div></div>
              <div className="tile"><div className="label">Realized P&amp;L</div><div className="value">{signedMoney(doc.paper.realized_pnl)}</div><div className="delta">{r(doc.paper.realized_r)} total · expectancy {paperExp === null ? "—" : r(paperExp)}</div></div>
              <div className="tile"><div className="label">Gate-fired shadow trades</div><div className="value">{closedGate}</div><div className="delta">expectancy {gateStats?.expectancy_r == null ? "—" : r(gateStats.expectancy_r)} · hit {pct(gateStats?.hit_rate, 0)}</div></div>
              <div className="tile"><div className="label">M1 record streak</div><div className="value">{recStreak}/10</div><div className="delta">complete days from the latest</div></div>
            </div>
          </div>
        </section>

        <section className="card two-thirds">
          <h2>Required vs realized growth</h2>
          <p className="hint">The plan's curve compounds {pct(growth.perDayRequired, 2)} every NYSE trading day ({growth.totalTradingDays} of them between {doc.goal.start_date} and {doc.goal.target_date}); realized is the paper ledger; the shadow line is what the engine's gate-fired trades would have made at real marks.</p>
          <GrowthChart g={growth} />
        </section>

        <section className="card third">
          <h2>Posterior p / W → size</h2>
          <p className="hint">Prior 30 %/5R worth n0 = 30 trades, edge shrunk toward breakeven (kelly.py). n = closed gate-fired trades.</p>
          <dl className="kv">
            <dt>n (gate-fired, closed)</dt><dd>{post.n}{post.n ? ` · weight ${pct(post.weight, 0)}` : " · prior only"}</dd>
            <dt>measured p̂ / Ŵ</dt><dd>{post.p_hat === null ? "—" : pct(post.p_hat, 0)} / {post.w_hat === null ? "—" : `${num(post.w_hat, 2)}R`}</dd>
            <dt>posterior p / W</dt><dd>{pct(post.p, 1)} / {num(post.w, 2)}R (breakeven {pct(post.p_be, 1)})</dd>
            <dt>full Kelly f*</dt><dd>{pct(post.f_full, 2)} of account{post.n === 0 ? " → one-contract floor" : ""}</dd>
            <dt>used (k = {k})</dt><dd>{pct(post.f_full * k, 2)} → {money(post.f_full * k * account)} of {money(account)}</dd>
            <dt>growth / trade</dt><dd>{pct(gPerTrade, 2)} full · {pct(gHalf, 2)} at k</dd>
            <dt>trades to {money(doc.goal.target)}</dt><dd>{post.n === 0 ? "needs closed gate-fired trades (prior edge is ε only)" : toTargetFull === null ? "— (no edge)" : `${Math.round(toTargetFull)} full · ${toTargetK === null ? "—" : Math.round(toTargetK)} at k`}</dd>
            <dt>account · k</dt><dd>{money(account)} · {k}</dd>
          </dl>
        </section>

        <section className="card half">
          <h2>R distribution — closed trades</h2>
          <p className="hint">Gate-fired shadows feed the posterior; paper fills are what the sandbox actually did; fast-lane hypotheses run with zero capital.</p>
          <RHistogram series={[
            { label: "engine · gate", color: "var(--series-1)", values: doc.r_values.gate },
            { label: "paper (sandbox)", color: "var(--series-2)", values: doc.r_values.paper },
            { label: "fast-lane hypotheses", color: "var(--series-3)", values: doc.r_values.fast_lane },
          ]} />
        </section>

        <section className="card half">
          <h2>Expectancy by source</h2>
          <p className="hint">Last {doc.window_days} days. Fast-lane expectancy {fastExp === null ? "—" : r(fastExp)} over {doc.r_values.fast_lane.length}; modeled M1 shadows (Black-Scholes) listed for history, not sizing.</p>
          {doc.stats.length ? (
            <table>
              <thead><tr><th>Source</th><th>Lane</th><th className="num">n</th><th className="num">Hit</th><th className="num">Avg win</th><th className="num">Avg loss</th><th className="num">Expectancy</th><th className="num">Total</th></tr></thead>
              <tbody>
                {[...doc.stats].sort((a, b) => (b.n ?? 0) - (a.n ?? 0)).map((s, i) => (
                  <tr key={i}>
                    <td>{s.source === "engine" ? `engine · ${s.engine_source ?? ""}` : `${s.source} (modeled)`}</td>
                    <td>{s.lane ?? "—"}</td><td className="num">{s.n}</td><td className="num">{pct(s.hit_rate, 0)}</td><td className="num">{s.avg_win_r == null ? "—" : r(s.avg_win_r)}</td>
                    <td className="num">{s.avg_loss_r == null ? "—" : r(s.avg_loss_r)}</td><td className="num"><RBadge v={s.expectancy_r} /></td><td className="num">{s.total_r == null ? "—" : r(s.total_r)}</td>
                  </tr>
                ))}
                {doc.r_values.paper.length ? (
                  <tr><td>paper (sandbox)</td><td>—</td><td className="num">{doc.r_values.paper.length}</td><td className="num">{pct(doc.r_values.paper.filter((x) => x > 0).length / doc.r_values.paper.length, 0)}</td><td className="num">—</td><td className="num">—</td><td className="num"><RBadge v={paperExp} /></td><td className="num">{r(doc.paper.realized_r)}</td></tr>
                ) : null}
              </tbody>
            </table>
          ) : <p className="empty">No closed trades in the window.</p>}
        </section>

        <section className="card half">
          <h2>Brier by bucket</h2>
          <p className="hint">Brief probabilities vs what happened ({doc.calibration_n} resolved checklist{doc.calibration_n === 1 ? "" : "s"}). A calibrated forecaster's dots overlap.</p>
          <Calibration rows={doc.brier} />
        </section>

        <section className="card half">
          <h2>Paper execution — sandbox</h2>
          <p className="hint">Approval mode: every gate fire is proposed on Telegram; no answer in 3 min = Skip. Reconciled against the broker every 30 s.</p>
          <div className="tiles">
            <div className="tile"><div className="label">Proposals</div><div className="value">{apprTotal}</div><div className="delta">✅ {appr.approved ?? 0} · ⏭ {appr.skipped ?? 0} · ⏱ {appr.timeout ?? 0} · ✖ {appr.expired ?? 0}{doc.paper.avg_decision_latency_s != null ? ` · ${Math.round(doc.paper.avg_decision_latency_s)} s avg` : ""}</div></div>
            <div className="tile"><div className="label">Reconciliations (7 d)</div><div className="value">{recon.n}</div><div className={`delta ${recon.mismatches ? "serious" : ""}`}>{recon.mismatches ? `${recon.mismatches} mismatch(es)` : "all matched"}{recon.last ? ` · last ${et(recon.last.ts)}` : ""}</div></div>
            <div className="tile"><div className="label">By status</div><div className="value">{Object.values(doc.paper.by_status || {}).reduce((a, b) => a + b, 0)}</div><div className="delta">{Object.entries(doc.paper.by_status || {}).map(([s, n]) => `${s} ${n}`).join(" · ") || "—"}</div></div>
          </div>
          {doc.paper.recent.length ? (
            <table style={{ marginTop: 10 }}>
              <thead><tr><th>Date</th><th>Trade</th><th>Status</th><th className="num">Entry</th><th className="num">Exit</th><th className="num">R</th><th className="num">P&amp;L</th><th className="num">Slip in/out</th></tr></thead>
              <tbody>
                {doc.paper.recent.slice(0, 12).map((t) => (
                  <tr key={t.engine_key}>
                    <td>{t.trade_date.slice(5)}</td>
                    <td><span className="mono">{t.symbol} {t.option_symbol ?? ""}</span></td>
                    <td>{t.status}{t.decision_latency_s != null ? ` (${Math.round(t.decision_latency_s)} s)` : ""}{t.block_reason ? <span className="muted"> · {t.block_reason.split(":")[0]}</span> : null}</td>
                    <td className="num">{t.entry_qty ? `${t.entry_qty} @ ${num(t.entry_price)}` : "—"}</td>
                    <td className="num">{t.exit_qty ? `${t.exit_qty} @ ${num(t.exit_price)}` : "—"}</td>
                    <td className="num"><RBadge v={t.realized_r} /></td>
                    <td className="num">{signedMoney(t.realized_pnl)}</td>
                    <td className="num">{t.slippage_entry == null ? "—" : `${t.slippage_entry >= 0 ? "+" : ""}${num(t.slippage_entry)} / ${t.slippage_exit == null ? "—" : `${t.slippage_exit >= 0 ? "+" : ""}${num(t.slippage_exit)}`}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="empty">No paper trades yet. The first sandbox round trips (`./run.sh paper-roundtrip`) and the daemon's approved entries land here.</p>}
        </section>

        <section className="card">
          <h2>Ledger — last {doc.window_days} days</h2>
          <p className="hint">Every shadow trade (engine at real DXLink marks, M1 modeled) with its paper execution when there was one. Marked at the bid; R = (proceeds − cost) ÷ cost.</p>
          <Legend items={[{ label: "engine · gate (sized, counts for rails)", color: "var(--series-1)", kind: "swatch" }, { label: "paper fill", color: "var(--series-2)", kind: "swatch" }, { label: "fast lane (zero capital)", color: "var(--series-3)", kind: "swatch" }]} />
          {doc.ledger.length ? (
            <table>
              <thead><tr><th>Opened (ET)</th><th>Symbol</th><th>Source</th><th>Trade</th><th className="num">Entry→Exit</th><th className="num">R</th><th>Exit</th><th>Paper</th></tr></thead>
              <tbody>
                {doc.ledger.slice(0, 60).map((row) => (
                  <tr key={`${row.id}-${row.engine_key}`}>
                    <td>{et(row.opened_at, true)}</td>
                    <td><span className="swatch" style={{ background: row.engine_source === "gate" ? "var(--series-1)" : row.engine_source === "fast_lane" ? "var(--series-3)" : "var(--text-muted)", display: "inline-block", width: 8, height: 8, borderRadius: 2, marginRight: 6 }} />{row.symbol}</td>
                    <td>{sourceLabel(row)}</td>
                    <td className="mono">{row.direction} {row.option_type} {row.option_symbol ?? `${row.strike ?? ""}`}{row.contracts ? ` ×${row.contracts}` : ""}</td>
                    <td className="num">{num(row.entry_premium)}→{num(row.exit_premium)}</td>
                    <td className="num"><RBadge v={row.r_result} /></td>
                    <td>{row.exit_reason ?? row.status}</td>
                    <td>{row.paper_status ? <span><span className="swatch" style={{ background: "var(--series-2)", display: "inline-block", width: 8, height: 8, borderRadius: 2, marginRight: 6 }} />{row.paper_status}{row.realized_r != null ? ` ${r(row.realized_r)}` : ""}</span> : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="empty">The ledger is empty for this window.</p>}
        </section>

        <section className="card half">
          <h2>Today — {doc.today.date}</h2>
          <dl className="kv">
            <dt>Checklists (brief)</dt><dd>{doc.today.checklists}</dd>
            <dt>Engine decisions</dt><dd>{Object.entries(doc.today.decisions || {}).map(([k2, v]) => `${k2} ${v}`).join(" · ") || "none yet"}</dd>
            <dt>Stand-downs</dt><dd>{Object.entries(doc.today.stand_downs || {}).sort((a, b) => b[1] - a[1]).map(([k2, v]) => `${k2} ${v}`).join(" · ") || "—"}</dd>
            <dt>Last run</dt><dd>{daemon ? `${daemon.run_id} · ${daemon.status} · ${daemon.host ?? ""} · feed ${daemon.engine_feed_mode ?? feedMode} · ${daemon.engine_counts?.evaluations ?? 0} evals · ${daemon.engine_counts?.fired ?? 0} fired · ${daemon.engine_counts?.fast_lane_opens ?? 0} fast-lane` : "none"}</dd>
            <dt>Paper (last run)</dt><dd>{daemon?.execution ? JSON.stringify((daemon.execution as { counts?: unknown }).counts ?? daemon.execution).slice(0, 160) : "no execution in the last run"}</dd>
          </dl>
        </section>

        <section className="card half">
          <h2>Daily records (M1 gate)</h2>
          <table>
            <thead><tr><th>Date</th><th>Brief</th><th className="num">Checklists</th><th className="num">Shadows</th><th className="num">Open</th><th>Complete</th></tr></thead>
            <tbody>
              {doc.daily_records.slice(0, 10).map((d) => (
                <tr key={d.trade_date}><td>{d.trade_date}</td><td>{d.has_brief ? "yes" : "no"}</td><td className="num">{d.checklists}</td><td className="num">{d.shadow_trades}</td><td className="num">{d.still_open}</td><td>{d.complete ? "✓" : "—"}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
      <footer>Read-only. Data: Supabase project Quant edge, schema saa, via public.saa_dashboard (service role, server side). Tier 1 rails are code; Tier 2 lives in saa.rules.</footer>
    </main>
  );
}
