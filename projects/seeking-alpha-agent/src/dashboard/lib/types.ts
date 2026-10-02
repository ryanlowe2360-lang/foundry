/** The shape of one `public.saa_dashboard(p_days)` document (migration 0008). Everything optional-ish: the page must render an
 * empty ledger gracefully (day one) and never throw on a null. */

export interface LedgerRow {
  id: number | null;
  engine_key: string | null;
  trade_date: string;
  symbol: string;
  lane: string | null;
  source: string | null; // engine | brief | tv | manual
  engine_source: string | null; // gate | fast_lane
  window_name: string | null;
  direction: string | null;
  option_type: string | null;
  option_symbol: string | null;
  strike: number | null;
  contracts: number | null;
  opened_at: string;
  exit_at: string | null;
  entry_premium: number | null;
  exit_premium: number | null;
  r_result: number | null;
  exit_reason: string | null;
  status: string;
  probability_hint: number | null;
  mfe_r: number | null;
  mae_r: number | null;
  model_version: string | null;
  shadow_pnl: number | null;
  paper_status: string | null;
  decision: string | null;
  decision_latency_s: number | null;
  entry_qty: number | null;
  entry_price: number | null;
  exit_qty: number | null;
  exit_price: number | null;
  realized_pnl: number | null;
  realized_r: number | null;
  slippage_entry: number | null;
  slippage_exit: number | null;
  block_reason: string | null;
}

export interface StatRow {
  source: string | null;
  engine_source: string | null;
  lane: string | null;
  n: number;
  hit_rate: number | null;
  avg_win_r: number | null;
  avg_loss_r: number | null;
  expectancy_r: number | null;
  total_r: number | null;
  voided: number;
}

export interface BrierRow {
  bucket: string;
  n: number;
  avg_p: number;
  hit_rate: number;
  brier: number;
}

export interface PaperRecent {
  engine_key: string;
  trade_date: string;
  symbol: string;
  option_symbol: string | null;
  status: string;
  block_reason: string | null;
  decision: string | null;
  decision_latency_s: number | null;
  entry_qty: number | null;
  entry_price: number | null;
  exit_qty: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  realized_pnl: number | null;
  realized_r: number | null;
  shadow_r: number | null;
  slippage_entry: number | null;
  slippage_exit: number | null;
  created_at: string;
}

export interface Dashboard {
  generated_at: string;
  window_days: number;
  settings: Record<string, string> | null;
  goal: { start_date: string; start_equity: number; target: number; target_date: string };
  ledger: LedgerRow[];
  stats: StatRow[];
  r_values: { gate: number[]; fast_lane: number[]; modeled: number[]; paper: number[] };
  brier: BrierRow[];
  calibration_n: number;
  paper: {
    trades: number;
    closed: number;
    open: number;
    realized_pnl: number;
    realized_r: number;
    by_status: Record<string, number>;
    approvals: Record<string, number>;
    avg_decision_latency_s: number | null;
    reconciliations: { n: number; mismatches: number; last: { ts: string; ok: boolean; mismatches: unknown[]; open_trades: number | null; error: string | null } | null };
    equity_curve: { date: string; pnl: number; cum: number }[];
    recent: PaperRecent[];
  };
  daily_records: { trade_date: string; has_brief: boolean; checklists: number; shadow_trades: number; still_open: number; complete: boolean }[];
  daemon: {
    run_id: string;
    trade_date: string;
    started_at: string;
    ended_at: string | null;
    last_seen: string;
    status: string;
    host: string | null;
    version: string | null;
    feed: { mode?: string; lag_s?: number | null; events?: number; reconnects?: number } | null;
    engine_counts: Record<string, number> | null;
    engine_feed_mode: string | null;
    execution: Record<string, unknown> | null;
  } | null;
  today: { date: string; decisions: Record<string, number>; stand_downs: Record<string, number>; checklists: number };
}
