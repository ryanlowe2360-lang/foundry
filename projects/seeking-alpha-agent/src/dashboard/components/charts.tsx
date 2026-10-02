import { money, r } from "@/lib/format";
import type { Growth } from "@/lib/growth";

/** Shared helpers for the SVG charts. Everything renders on the server: no client JS, native <title> tooltips per mark,
 *  a table view under each chart (details/summary) so every value is reachable without color. */

export function Legend({ items }: { items: { label: string; color: string; kind?: "line" | "swatch" }[] }) {
  return (
    <div className="legend" role="list">
      {items.map((it) => (
        <span key={it.label} role="listitem">
          <span className={it.kind === "swatch" ? "swatch" : "key"} style={{ background: it.color }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min];
  const span = max - min;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = start; v <= max + 1e-9; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

// ------------------------------------------------------------------------------------------------- R histogram
const BIN_EDGES = [-1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6];

function binIndex(v: number): number {
  if (v >= BIN_EDGES[BIN_EDGES.length - 1]) return BIN_EDGES.length - 1;
  for (let i = 0; i < BIN_EDGES.length - 1; i++) if (v < BIN_EDGES[i + 1]) return i;
  return BIN_EDGES.length - 1;
}

function binLabel(i: number): string {
  if (i === BIN_EDGES.length - 1) return `${BIN_EDGES[i]}+`;
  const a = BIN_EDGES[i], b = BIN_EDGES[i + 1];
  return `${a}…${b}`;
}

/** Short axis tick: the bin's lower edge ("−.75", "0", "1.5", "6+") — the full range lives in the tooltip and the table. */
function binTick(i: number): string {
  if (i === BIN_EDGES.length - 1) return `${BIN_EDGES[i]}+`;
  const a = BIN_EDGES[i];
  const s = Math.abs(a) < 1 && a !== 0 ? String(a).replace("0.", ".") : String(a);
  return s.replace("-", "−");
}

export function RHistogram({ series }: { series: { label: string; color: string; values: number[] }[] }) {
  const present = series.filter((s) => s.values.length > 0);
  const counts = present.map((s) => {
    const c = new Array(BIN_EDGES.length).fill(0) as number[];
    s.values.forEach((v) => (c[binIndex(v)] += 1));
    return c;
  });
  const maxCount = Math.max(1, ...counts.flat());
  const W = 760, H = 220, padL = 34, padR = 10, padT = 10, padB = 36;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const nb = BIN_EDGES.length;
  const slot = plotW / nb;
  const groups = Math.max(1, present.length);
  const barW = Math.min(24, (slot - 6) / groups - 2);
  const y = (c: number) => padT + plotH - (c / maxCount) * plotH;
  const ticks = niceTicks(0, maxCount, 3).filter((t) => Number.isInteger(t));
  const zeroX = padL + 4 * slot; // the 0R edge sits between bin 3 (−0.25…0) and bin 4 (0…0.25)
  if (!present.length) return <p className="empty">No closed trades yet — the histogram fills as the ledger closes trades.</p>;
  return (
    <>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Distribution of closed-trade results in R">
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid" x1={padL} x2={W - padR} y1={y(t)} y2={y(t)} />
            <text x={padL - 6} y={y(t) + 4} textAnchor="end">{t}</text>
          </g>
        ))}
        <line className="axis" x1={padL} x2={W - padR} y1={padT + plotH} y2={padT + plotH} />
        <line x1={zeroX} x2={zeroX} y1={padT} y2={padT + plotH} stroke="var(--text-muted)" strokeWidth={1} />
        {BIN_EDGES.map((_, i) => (
          <text key={i} x={padL + i * slot} y={H - padB + 14} textAnchor="middle">{binTick(i)}</text>
        ))}
        {present.map((s, si) =>
          counts[si].map((c, i) => {
            if (!c) return null;
            const x = padL + i * slot + (slot - (barW * groups + 2 * (groups - 1))) / 2 + si * (barW + 2);
            const top = y(c), h = padT + plotH - top;
            return (
              <g key={`${si}-${i}`}>
                <rect x={x} y={top} width={barW} height={h} fill={s.color} rx={4} ry={4} />
                <rect x={x} y={padT + plotH - Math.min(4, h)} width={barW} height={Math.min(4, h)} fill={s.color} />
                <title>{`${s.label}: ${c} trade${c === 1 ? "" : "s"} in ${binLabel(i)}R`}</title>
              </g>
            );
          }),
        )}
        <text x={padL} y={H - 4} className="muted">bins by lower edge in R · R = result ÷ premium paid (−1R = full loss) · the vertical line is 0R</text>
      </svg>
      <Legend items={present.map((s) => ({ label: `${s.label} (n=${s.values.length})`, color: s.color, kind: "swatch" }))} />
      <details>
        <summary>Table view</summary>
        <table>
          <thead><tr><th>Bin (R)</th>{present.map((s) => <th key={s.label} className="num">{s.label}</th>)}</tr></thead>
          <tbody>
            {BIN_EDGES.map((_, i) => (
              <tr key={i}><td>{binLabel(i)}</td>{counts.map((c, si) => <td key={si} className="num">{c[i]}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  );
}

// ------------------------------------------------------------------------------------------------ growth chart
export function GrowthChart({ g }: { g: Growth }) {
  const pts = g.points;
  const W = 760, H = 260, padL = 54, padR = 96, padT = 12, padB = 28;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xs = pts.map((_, i) => padL + (pts.length > 1 ? (i / (pts.length - 1)) * plotW : plotW / 2));
  const values = pts.flatMap((p) => [p.required, p.realized ?? NaN, p.shadow ?? NaN]).filter((v) => Number.isFinite(v));
  const lo = Math.min(...values) * 0.98, hi = Math.max(...values) * 1.02;
  const y = (v: number) => padT + plotH - ((v - lo) / Math.max(1e-9, hi - lo)) * plotH;
  const path = (key: "required" | "realized" | "shadow") =>
    pts.map((p, i) => {
      const v = p[key];
      return v === null || !Number.isFinite(v) ? null : `${i === 0 ? "M" : "L"}${xs[i].toFixed(1)},${y(v).toFixed(1)}`;
    }).filter(Boolean).join(" ");
  const ticks = niceTicks(lo, hi, 4);
  const last = pts[pts.length - 1];
  const labelAt = (v: number | null, text: string) => (v === null ? null : <text x={W - padR + 6} y={y(v) + 4} className="label">{text}</text>);
  const dateTicks = pts.filter((_, i) => i === 0 || i === pts.length - 1 || (pts.length > 6 && i % Math.ceil(pts.length / 5) === 0));
  return (
    <>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Required versus realized equity since the start date">
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid" x1={padL} x2={W - padR} y1={y(t)} y2={y(t)} />
            <text x={padL - 6} y={y(t) + 4} textAnchor="end">{money(t)}</text>
          </g>
        ))}
        <line className="axis" x1={padL} x2={W - padR} y1={padT + plotH} y2={padT + plotH} />
        {dateTicks.map((p) => {
          const i = pts.indexOf(p);
          return <text key={p.date} x={xs[i]} y={H - padB + 16} textAnchor={i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle"}>{p.date.slice(5)}</text>;
        })}
        <path d={path("shadow")} fill="none" stroke="var(--series-3)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        <path d={path("realized")} fill="none" stroke="var(--series-2)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        <path d={path("required")} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {pts.map((p, i) => (
          <g key={p.date}>
            <circle cx={xs[i]} cy={y(p.required)} r={2.5} fill="var(--series-1)">
              <title>{`${p.date} · required ${money(p.required)} · realized ${money(p.realized)} · shadow(gate) ${money(p.shadow)}`}</title>
            </circle>
          </g>
        ))}
        {[
          { v: last.required, c: "var(--series-1)" },
          { v: last.realized, c: "var(--series-2)" },
          { v: last.shadow, c: "var(--series-3)" },
        ].map((m, i) => (m.v === null ? null : <circle key={i} cx={xs[xs.length - 1]} cy={y(m.v)} r={4} fill={m.c} stroke="var(--surface-1)" strokeWidth={2} />))}
        {labelAt(last.required, `req ${money(last.required)}`)}
        {last.realized !== null && Math.abs(y(last.realized) - y(last.required)) > 12 ? labelAt(last.realized, `paper ${money(last.realized)}`) : null}
      </svg>
      <Legend items={[{ label: `required path (${(g.perDayRequired * 100).toFixed(2)} %/trading day over ${g.totalTradingDays} NYSE days)`, color: "var(--series-1)" }, { label: "realized — paper ledger", color: "var(--series-2)" }, { label: "shadow — gate-fired, if real", color: "var(--series-3)" }]} />
      <details>
        <summary>Table view</summary>
        <table>
          <thead><tr><th>Date</th><th className="num">Required</th><th className="num">Realized (paper)</th><th className="num">Shadow (gate)</th></tr></thead>
          <tbody>
            {[...pts].reverse().slice(0, 15).map((p) => (
              <tr key={p.date}><td>{p.date}</td><td className="num">{money(p.required, 0)}</td><td className="num">{money(p.realized, 0)}</td><td className="num">{money(p.shadow, 0)}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  );
}

// ------------------------------------------------------------------------------------------------- calibration
export function Calibration({ rows }: { rows: { bucket: string; n: number; avg_p: number; hit_rate: number; brier: number }[] }) {
  if (!rows.length) return <p className="empty">No resolved checklists yet — Brier by bucket appears after the first tallies.</p>;
  const W = 760, H = 60 + rows.length * 34, padL = 70, padR = 120, padT = 10, padB = 28;
  const plotW = W - padL - padR;
  const x = (v: number) => padL + v * plotW;
  return (
    <>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Forecast probability versus realized hit rate by bucket">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line className="grid" x1={x(t)} x2={x(t)} y1={padT} y2={H - padB} />
            <text x={x(t)} y={H - padB + 14} textAnchor="middle">{Math.round(t * 100)}%</text>
          </g>
        ))}
        {rows.map((b, i) => {
          const cy = padT + 20 + i * 34;
          return (
            <g key={b.bucket}>
              <text x={padL - 8} y={cy + 4} textAnchor="end" className="label">{b.bucket}</text>
              <line x1={x(Math.min(b.avg_p, b.hit_rate))} x2={x(Math.max(b.avg_p, b.hit_rate))} y1={cy} y2={cy} stroke="var(--line)" strokeWidth={2} />
              <circle cx={x(b.avg_p)} cy={cy} r={5} fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth={2}>
                <title>{`${b.bucket}: forecast ${(b.avg_p * 100).toFixed(0)} % · hit ${(b.hit_rate * 100).toFixed(0)} % · n=${b.n} · Brier ${b.brier.toFixed(3)}`}</title>
              </circle>
              <circle cx={x(b.hit_rate)} cy={cy} r={5} fill="var(--series-2)" stroke="var(--surface-1)" strokeWidth={2}>
                <title>{`${b.bucket}: hit rate ${(b.hit_rate * 100).toFixed(0)} % (n=${b.n})`}</title>
              </circle>
              <text x={W - padR + 6} y={cy + 4}>{`n=${b.n} · Brier ${b.brier.toFixed(3)}`}</text>
            </g>
          );
        })}
      </svg>
      <Legend items={[{ label: "forecast p (brief)", color: "var(--series-1)", kind: "swatch" }, { label: "realized hit rate", color: "var(--series-2)", kind: "swatch" }]} />
      <details>
        <summary>Table view</summary>
        <table>
          <thead><tr><th>Bucket</th><th className="num">n</th><th className="num">avg p</th><th className="num">hit rate</th><th className="num">Brier</th></tr></thead>
          <tbody>{rows.map((b) => <tr key={b.bucket}><td>{b.bucket}</td><td className="num">{b.n}</td><td className="num">{(b.avg_p * 100).toFixed(0)}%</td><td className="num">{(b.hit_rate * 100).toFixed(0)}%</td><td className="num">{b.brier.toFixed(4)}</td></tr>)}</tbody>
        </table>
      </details>
    </>
  );
}

export function RBadge({ v }: { v: number | null | undefined }) {
  if (v === null || v === undefined || !Number.isFinite(v)) return <span className="muted">—</span>;
  const z = Math.abs(v) < 0.005;
  return <span style={{ color: z ? "inherit" : v > 0 ? "var(--good)" : "var(--serious)", fontWeight: 600 }}>{z ? "0.00R" : r(v)}</span>;
}
