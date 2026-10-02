/** Required-vs-realized growth toward the goal ($1,000 → $5,000,000 by 2027-09-25). "Required" compounds evenly over the
 *  NYSE trading days between the start and the target (≈3.44 % per trading day, the plan's figure); "realized" is the
 *  start equity plus the paper ledger's cumulative realized P&L; "shadow (gate)" is what the engine's gate-fired shadow
 *  trades would have made, for context. Calendar: NYSE holidays 2026–27 as in the daemon's clock.py. */

const NYSE_HOLIDAYS = new Set([
  "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
  "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
]);

export function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function isTradingDay(isoDate: string): boolean {
  const d = new Date(isoDate + "T12:00:00Z");
  const dow = d.getUTCDay();
  return dow >= 1 && dow <= 5 && !NYSE_HOLIDAYS.has(isoDate);
}

/** Trading days strictly after `from` up to and including `to`. */
export function tradingDays(from: string, to: string): string[] {
  const out: string[] = [];
  const d = new Date(from + "T12:00:00Z");
  const end = new Date(to + "T12:00:00Z");
  while (d < end) {
    d.setUTCDate(d.getUTCDate() + 1);
    const s = iso(d);
    if (isTradingDay(s)) out.push(s);
  }
  return out;
}

export interface GrowthPoint {
  date: string;
  required: number;
  realized: number | null;
  shadow: number | null;
}

export interface Growth {
  points: GrowthPoint[];
  totalTradingDays: number;
  elapsedTradingDays: number;
  perDayRequired: number; // e.g. 0.0344
  requiredToday: number;
  realizedToday: number;
  shadowToday: number;
  targetDate: string;
  target: number;
}

export function growthSeries(goal: { start_date: string; start_equity: number; target: number; target_date: string }, today: string,
                             paperCurve: { date: string; cum: number }[], shadowByDate: Map<string, number>): Growth {
  const all = tradingDays(goal.start_date, goal.target_date);
  const total = all.length;
  const ratio = goal.target / goal.start_equity;
  const perDay = Math.pow(ratio, 1 / total) - 1;
  const upTo = all.filter((d) => d <= today);
  const paper = new Map(paperCurve.map((p) => [p.date, p.cum]));
  const points: GrowthPoint[] = [];
  let cumPaper = 0;
  let cumShadow = 0;
  let paperSeen = false;
  let shadowSeen = false;
  const first: GrowthPoint = { date: goal.start_date, required: goal.start_equity, realized: goal.start_equity, shadow: goal.start_equity };
  points.push(first);
  upTo.forEach((d, i) => {
    if (paper.has(d)) {
      cumPaper = paper.get(d) as number;
      paperSeen = true;
    }
    if (shadowByDate.has(d)) {
      cumShadow += shadowByDate.get(d) as number;
      shadowSeen = true;
    }
    points.push({
      date: d,
      required: goal.start_equity * Math.pow(ratio, (i + 1) / total),
      realized: goal.start_equity + cumPaper,
      shadow: goal.start_equity + cumShadow,
    });
  });
  void paperSeen;
  void shadowSeen;
  const last = points[points.length - 1];
  return { points, totalTradingDays: total, elapsedTradingDays: upTo.length, perDayRequired: perDay, requiredToday: last.required,
           realizedToday: last.realized ?? goal.start_equity, shadowToday: last.shadow ?? goal.start_equity, targetDate: goal.target_date, target: goal.target };
}
