-- 0007_engine_ledger.sql — M3 rules engine: shadow ledger on real marks, decision journal, rules v2, RPCs (2026-10-01).
-- Re-runnable. Project: Quant edge (zspbkcheounkwnpjkgrv), schema `saa`.
--
-- The daemon's engine (src/daemon/saa_daemon/engine/) writes its shadow trades into the existing saa.shadow_trades
-- table (source = 'engine', model_version = 'dxlink-marks-v1') so v_shadow_stats / the tally / the Friday review see
-- M1's modeled shadows and M3's real-mark shadows side by side (D5 "revisit" measurement). Rows are keyed by the
-- engine's deterministic `engine_key` so a replay or a mirror retry upserts instead of duplicating.

-- ---------------------------------------------------------------------------
-- shadow_trades: engine columns
-- ---------------------------------------------------------------------------
alter table saa.shadow_trades add column if not exists engine_key      text;
alter table saa.shadow_trades add column if not exists contracts       int;
alter table saa.shadow_trades add column if not exists size_r          numeric;          -- cost / account at entry
alter table saa.shadow_trades add column if not exists sizing          jsonb;            -- Kelly module output (mode, f*, caps, reasons)
alter table saa.shadow_trades add column if not exists gates           jsonb;            -- six-gate evaluation at entry (gate-fired rows)
alter table saa.shadow_trades add column if not exists option_symbol   text;             -- DXLink streamer symbol, e.g. .SPY260928C650
alter table saa.shadow_trades add column if not exists entry_bid       numeric;
alter table saa.shadow_trades add column if not exists entry_iv        numeric;
alter table saa.shadow_trades add column if not exists exit_note       text;
alter table saa.shadow_trades add column if not exists banked_contracts int;
alter table saa.shadow_trades add column if not exists banked_proceeds  numeric;
alter table saa.shadow_trades add column if not exists counts_for_rails boolean not null default true;
alter table saa.shadow_trades add column if not exists events          jsonb;
alter table saa.shadow_trades add column if not exists engine_source   text;             -- gate | fast_lane
alter table saa.shadow_trades add column if not exists updated_at      timestamptz not null default now();
create unique index if not exists shadow_trades_engine_key_idx on saa.shadow_trades (engine_key) where engine_key is not null;

-- exit_reason gains the engine's reasons (free text column already; documented here):
--   trail | time_stop | expiry | release_blackout | vwap_loss | failed_extreme | volume_taper | session_end | void_no_marks

-- ---------------------------------------------------------------------------
-- Decision journal: every gate evaluation, stand-down, fire, fast-lane open/skip, close.
-- ---------------------------------------------------------------------------
create table if not exists saa.engine_decisions (
  id          bigserial primary key,
  ts          timestamptz not null,
  trade_date  date not null,
  run_id      text,
  symbol      text not null,
  window_name text,
  kind        text not null,            -- gate | fast_lane | manage
  decision    text not null,            -- fire | stand_down | open | skip | flag_only | close | void
  reason      text not null,
  payload     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists engine_decisions_date_idx on saa.engine_decisions (trade_date, symbol);
create index if not exists engine_decisions_ts_idx on saa.engine_decisions (ts desc);
alter table saa.engine_decisions enable row level security;
grant all on all tables in schema saa to service_role;
grant all on all sequences in schema saa to service_role;

-- ---------------------------------------------------------------------------
-- Tier 2 rules v2 — the engine's parameters. No edge-rule value changes from v1 (same bank/trail/lanes/thresholds);
-- new keys only (windows, gates, exits, max_concurrent_positions, max_alerts_per_day, mark window). The Tier 1 keys v1
-- carried (daily_stop_r, consecutive_loss_lockout, cooling_off, no_entry_et, spread_filter) are not repeated: Tier 1 is
-- code (saa_daemon/engine/tier1.py, D16) and the engine ignores them if present.
-- ---------------------------------------------------------------------------
insert into saa.rules (version, effective_from, params, evidence, created_by) values (2, date '2026-10-01', jsonb_build_object(
  'arming_thresholds', jsonb_build_object('partial', 15, 'armed', 28, 'max', 56),
  'partial_bank_frac_of_account', 0.075,
  'trail', jsonb_build_object('frac', 0.30, 'tight_frac', 0.20, 'tight_at_r', 3.0, 'atr_ticks', 10),
  'shadow', jsonb_build_object('strike_sigma_otm', 0.5, 'contracts', 1, 'min_spread_abs', 0.02,
                               'single_name_default_sigma_d', 0.025, 'last_entry_et', '15:30', 'mark_window_pct', 1.5),
  'windows', jsonb_build_object(
      'open',      jsonb_build_object('start', '09:30', 'entry_until', '09:58', 'stop', '10:00', 'data_day_entry_from', '09:35'),
      'mid',       jsonb_build_object('start', '10:00', 'entry_until', '11:30', 'stop', '11:30'),
      'afternoon', jsonb_build_object('start', '13:30', 'entry_until', '15:00', 'stop', '15:00'),
      'last_hour', jsonb_build_object('start', '15:00', 'entry_until', '15:40', 'stop', '15:55', 'require_negative_gamma', true),
      'event',     jsonb_build_object('entry_after_min', 5, 'entry_until_min', 15, 'stop_after_min', 55,
                                      'presser_offset_min', 30, 'presser_entry_min', 15, 'presser_stop_min', 60)),
  'fast_lanes', jsonb_build_object(
      'orb',          jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '10:30', 'range_minutes', 5, 'volume_mult', 1.5),
      'vwap',         jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '15:30', 'min_bars', 15),
      'continuation', jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '11:30', 'start_et', '10:00', 'volume_mult', 1.2, 'lookback', 10),
      'rvol',         jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '11:30', 'mult', 2.0),
      'compression',  jsonb_build_object('enabled', true,  'opens_shadow', false)),
  'gates', jsonb_build_object('readable_min_coverage', 0.5, 'favorable_index_regime', 'negative', 'wedge_rv_minutes', 60,
                              'wedge_rv_over_iv', 1.0, 'use_brief_wedge', true, 'require_checklist', true),
  'exits', jsonb_build_object(
      'vwap_loss',      jsonb_build_object('enabled', true, 'bars', 1, 'single_names_only', true),
      'volume_taper',   jsonb_build_object('enabled', true, 'bars', 3, 'frac', 0.5),
      'failed_extreme', jsonb_build_object('enabled', true)),
  'max_concurrent_positions', 3,
  'max_alerts_per_day', 20,
  'promotion_gate', jsonb_build_object('min_shadow_trades', 60, 'expectancy_gt', 0),
  'tier2_change_policy', jsonb_build_object('min_relevant_trades', 30, 'max_changes_per_week', 1)
), 'M3 build 2026-10-01: engine parameters added as new keys; every v1 edge value carried over unchanged (bank 7.5%, trail 30/20 at 3R, 0.5σ OTM, lanes). Tier 1 keys dropped from data — they are code (D16). Not a Friday-review change.', 'build-m3')
on conflict (version) do nothing;

-- ---------------------------------------------------------------------------
-- RPC surface for the engine (public schema, service_role only)
-- ---------------------------------------------------------------------------

-- Latest Tier 2 rules row.
create or replace function public.saa_rules_latest() returns jsonb
language sql security definer set search_path = saa, public as $$
  select to_jsonb(r) from (select version, effective_from, params, evidence, created_by, created_at from saa.rules order by version desc limit 1) r $$;

-- Today's (or p_date's) checklists from the 7:40 brief — the engine's Tier 3 inputs. Stand-downs included.
create or replace function public.saa_checklists_today(p_date date default null) returns jsonb
language sql security definer set search_path = saa, public as $$
  select coalesce(jsonb_agg(to_jsonb(c) order by c.id), '[]'::jsonb) from (
    select id, trade_date, symbol, window_kind, window_start, window_end, catalyst, catalyst_class, gates, gates_passed,
           arming_score, arming_max, direction, structure, thesis, invalidation, target_r, probability, size_r, decision, status
    from saa.checklists where trade_date = coalesce(p_date, saa.et(now())::date)) c $$;

-- The engine's closed, rail-counting (gate-fired) trades, oldest first — feeds the posterior and the edge-loss halt.
create or replace function public.saa_engine_ledger(p_limit int default 60) returns jsonb
language sql security definer set search_path = saa, public as $$
  select coalesce(jsonb_agg(to_jsonb(t) order by t.exit_at), '[]'::jsonb) from (
    select engine_key, trade_date, symbol, lane, r_result, exit_at, counts_for_rails
    from saa.shadow_trades
    where source = 'engine' and status = 'closed' and counts_for_rails and r_result is not null
    order by exit_at desc limit p_limit) t $$;

-- Upsert engine shadow-ledger rows (Position.row() payloads) by engine_key.
create or replace function public.saa_engine_shadow_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.shadow_trades (trade_date, symbol, lane, direction, source, opened_at, window_end, expiry, option_type, strike, sigma_d,
      entry_underlying, entry_mid, entry_premium, spread_frac, status, exit_at, exit_underlying, exit_premium, exit_reason, r_result,
      mfe_r, mae_r, hwm_premium, trail_activated, ticks_used, model_version, notes, checklist_id,
      engine_key, contracts, size_r, sizing, gates, option_symbol, entry_bid, entry_iv, exit_note, banked_contracts, banked_proceeds,
      counts_for_rails, events, engine_source, updated_at)
  select (r->>'trade_date')::date, upper(r->>'symbol'), r->>'lane', r->>'direction', 'engine', (r->>'opened_at')::timestamptz,
         (r->>'window_end')::timestamptz, ((r->>'expiration')::date + time '16:00') at time zone 'America/New_York', r->>'option_type',
         (r->>'strike')::numeric, coalesce((r->>'sigma_d')::numeric, 0), coalesce((r->>'entry_underlying')::numeric, 0),
         coalesce((r->>'entry_mid')::numeric, 0), coalesce((r->>'entry_premium')::numeric, 0), coalesce((r->>'spread_frac')::numeric, 0),
         case when r->>'status' in ('open','closed','void') then r->>'status' else 'open' end,
         nullif(r->>'exit_at','')::timestamptz, nullif(r->>'exit_underlying','')::numeric, nullif(r->>'exit_premium','')::numeric, r->>'exit_reason',
         nullif(r->>'r_result','')::numeric, nullif(r->>'mfe_r','')::numeric, nullif(r->>'mae_r','')::numeric, nullif(r->>'hwm_premium','')::numeric,
         coalesce((r->>'trail_activated')::boolean, false), nullif(r->>'marks','')::int, coalesce(r->>'model_version', 'dxlink-marks-v1'),
         r->>'window', nullif(r->>'checklist_id','')::bigint,
         r->>'engine_key', nullif(r->>'contracts','')::int, nullif(r->>'size_r','')::numeric, r->'sizing', r->'gates', r->>'option_symbol',
         nullif(r->>'entry_bid','')::numeric, nullif(r->>'entry_iv','')::numeric, r->>'exit_note', nullif(r->>'banked_contracts','')::int,
         nullif(r->>'banked_proceeds','')::numeric, coalesce((r->>'counts_for_rails')::boolean, true), r->'events', r->>'engine_source', now()
  from jsonb_array_elements(p_rows) r
  where (r->>'engine_key') is not null and (r->>'symbol') is not null
  on conflict (engine_key) where engine_key is not null do update set
    status = excluded.status, exit_at = excluded.exit_at, exit_underlying = excluded.exit_underlying, exit_premium = excluded.exit_premium,
    exit_reason = excluded.exit_reason, r_result = excluded.r_result, mfe_r = excluded.mfe_r, mae_r = excluded.mae_r,
    hwm_premium = excluded.hwm_premium, trail_activated = excluded.trail_activated, ticks_used = excluded.ticks_used,
    exit_note = excluded.exit_note, banked_contracts = excluded.banked_contracts, banked_proceeds = excluded.banked_proceeds,
    events = excluded.events, sizing = excluded.sizing, updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

-- Append decision-journal rows.
create or replace function public.saa_engine_decisions_insert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.engine_decisions (ts, trade_date, run_id, symbol, window_name, kind, decision, reason, payload)
  select (r->>'ts')::timestamptz, (r->>'trade_date')::date, r->>'run_id', upper(r->>'symbol'), r->>'window', r->>'kind', r->>'decision',
         left(r->>'reason', 400), r - 'trade_date' - 'run_id'
  from jsonb_array_elements(p_rows) r
  where (r->>'ts') is not null and (r->>'symbol') is not null and (r->>'trade_date') is not null;
  get diagnostics n = row_count;
  return n;
end $$;

-- Lock the new RPCs down to the service role (same pattern as 0001/0002/0005).
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname in ('saa_rules_latest','saa_checklists_today','saa_engine_ledger',
                                                        'saa_engine_shadow_upsert','saa_engine_decisions_insert') loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    execute format('grant execute on function %s to service_role', f.sig);
  end loop;
end $$;

-- Retention: engine_decisions should join the housekeeping cron (120 days). NOT APPLIED on 2026-10-01: the Supabase MCP
-- tool refuses statements containing `delete` without an interactive confirmation (the part above was applied as
-- migration 0007_engine_ledger; this part is pending). Run from the SQL editor when convenient — the table is small
-- (a few hundred rows per session day) so nothing depends on it:
--
--   select cron.alter_job((select jobid from cron.job where jobname = 'saa_housekeeping'), command := $$
--     delete from saa.price_ticks where ts < now() - interval '90 days';
--     delete from saa.run_log where ts < now() - interval '60 days';
--     delete from saa.market_snapshots where ts < now() - interval '180 days';
--     delete from saa.bars_1m where bar_time < now() - interval '120 days';
--     delete from saa.chain_snapshots where ts < now() - interval '45 days';
--     delete from saa.halts where halt_time < now() - interval '180 days';
--     delete from saa.daemon_runs where started_at < now() - interval '180 days';
--     delete from saa.engine_decisions where ts < now() - interval '120 days'$$);
