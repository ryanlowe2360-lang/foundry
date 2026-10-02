/** A faithful port of `saa_daemon/engine/kelly.py` (plan §0.2–0.3). Same numbers as `./run.sh kelly-table`:
 *  (25 %, 4R) → 6 %, (30 %, 5R) → 16 %, (35 %, 6R) → 24 %; n = 0 → ε·(1 + 1/W) ≈ 0.6 % (the one-contract floor).
 *  Tier 1 values mirror tier1.py and are display-only here — the daemon is the source of truth. */

export const TIER1 = { shrink_n0: 30, prior_p: 0.3, prior_w: 5.0, edge_epsilon: 0.005, kelly_k_min: 0.5, kelly_k_max: 1.0 } as const;

export function breakevenP(w: number): number {
  return 1 / (1 + w);
}

export function kellyFraction(p: number, w: number): number {
  if (w <= 0) return 0;
  return Math.max(0, p - (1 - p) / w);
}

export function growthPerTrade(p: number, w: number, f: number): number {
  if (f <= 0) return 0;
  if (f >= 1) return -Infinity;
  return p * Math.log1p(f * w) + (1 - p) * Math.log1p(-f);
}

export function tradesToMultiple(growth: number, multiple: number): number | null {
  return growth > 0 ? Math.log(multiple) / growth : null;
}

export interface Posterior {
  n: number;
  p_hat: number | null;
  w_hat: number | null;
  p_mix: number;
  w: number;
  p_be: number;
  p: number;
  f_full: number;
  weight: number;
}

export function posterior(resultsR: number[]): Posterior {
  const rs = resultsR.map(Number).filter((x) => Number.isFinite(x));
  const n = rs.length;
  const wins = rs.filter((r) => r > 0);
  const p_hat = n ? wins.length / n : null;
  const w_hat = wins.length ? wins.reduce((a, b) => a + b, 0) / wins.length : null;
  const n0 = TIER1.shrink_n0;
  const weight = n ? n / (n + n0) : 0;
  const p_mix = n ? (n * (p_hat as number) + n0 * TIER1.prior_p) / (n + n0) : TIER1.prior_p;
  const w = n ? (n * (w_hat ?? TIER1.prior_w) + n0 * TIER1.prior_w) / (n + n0) : TIER1.prior_w;
  const p_be = breakevenP(w);
  let p = p_be + TIER1.edge_epsilon + (p_mix - p_be) * weight;
  p = Math.min(Math.max(p, 0), 1);
  return { n, p_hat, w_hat, p_mix, w, p_be, p, f_full: kellyFraction(p, w), weight };
}

export function expectancy(rs: number[]): number | null {
  const xs = rs.filter((x) => Number.isFinite(x));
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
}
