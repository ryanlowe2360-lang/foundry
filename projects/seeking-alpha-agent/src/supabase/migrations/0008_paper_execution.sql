-- 0008_paper_execution.sql — M4 paper execution: paper trades / orders / approvals / reconciliations, their RPCs,
-- and the read-only dashboard RPC (2026-10-02). Re-runnable. Project: Quant edge (zspbkcheounkwnpjkgrv), schema `saa`.
--
-- The daemon's executor (src/daemon/saa_daemon/execution/) mirrors its SQLite book through four upsert RPCs
-- (service_role only, idempotent). `saa_dashboard(p_days)` is the single read the Next.js dashboard makes — one JSON
-- document with the ledger, the R distribution, expectancy by source, the posterior inputs, Brier by bucket, the
-- paper equity curve and the daemon status. No `public` views, no anon access.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table if not exists saa.paper_trades (
  engine_key         text primary key,                 -- = saa.shadow_trades.engine_key (the engine position this trade executes)
  trade_date         date not null,
  run_id             text,
  symbol             text not null,
  option_symbol      text,                             -- streamer symbol (.SPY260928C654)
  occ_symbol         text,                             -- OCC symbol the order carried
  direction          text,
  option_type        text,
  window_name        text,
  lane               text,
  contracts          int,                              -- planned (engine sizing)
  account            numeric,
  status             text not null,                    -- blocked | refused | proposed | skipped | timeout | expired | failed | working | open | closing | closed | unfilled | rejected | error | cancelled | halted
  block_reason       text,
  proposal_id        text,
  decision           text,
  decided_at         timestamptz,
  decision_latency_s numeric,
  entry_qty          int,
  entry_price        numeric,
  entry_at           timestamptz,
  exit_qty           int,
  exit_price         numeric,
  exit_at            timestamptz,
  exit_reason        text,
  shadow_entry_bid   numeric,
  shadow_entry_ask   numeric,
  shadow_exit_bid    numeric,
  shadow_r           numeric,
  realized_pnl       numeric,
  realized_r         numeric,
  slippage_entry     numeric,
  slippage_exit      numeric,
  fees               numeric,
  sizing_mode        text,
  probability        numeric,
  notes              jsonb not null default '[]'::jsonb,
  payload            jsonb not null default '{}'::jsonb,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index if not exists paper_trades_date_idx on saa.paper_trades (trade_date desc);
create index if not exists paper_trades_status_idx on saa.paper_trades (status);

create table if not exists saa.paper_orders (
  ticket_id          text primary key,
  engine_key         text not null,
  trade_date         date not null,
  run_id             text,
  symbol             text not null,
  occ_symbol         text,
  side               text,
  action             text,
  kind               text,                             -- entry | exit | bank | flatten
  quantity           int,
  status             text not null,                    -- new | working | filled | partial | unfilled | cancelled | rejected | error
  mark_bid           numeric,
  mark_ask           numeric,
  limit_prices       jsonb not null default '[]'::jsonb,   -- the ladder, null = market
  broker_order_ids   jsonb not null default '[]'::jsonb,
  filled_quantity    int,
  avg_fill_price     numeric,
  fills              jsonb not null default '[]'::jsonb,
  fees               numeric,
  reason             text,
  created_at         timestamptz,
  done_at            timestamptz,
  payload            jsonb not null default '{}'::jsonb,
  updated_at         timestamptz not null default now()
);
create index if not exists paper_orders_date_idx on saa.paper_orders (trade_date desc);
create index if not exists paper_orders_key_idx on saa.paper_orders (engine_key);

create table if not exists saa.approvals (
  proposal_id        text primary key,
  engine_key         text not null,
  trade_date         date not null,
  run_id             text,
  symbol             text,
  text               text,
  sent_at            timestamptz,
  message_id         bigint,
  timeout_s          numeric,
  status             text not null,                    -- pending | approved | skipped | timeout | expired | failed
  decided_at         timestamptz,
  decided_by         text,
  latency_s          numeric,
  note               text,
  payload            jsonb not null default '{}'::jsonb,
  updated_at         timestamptz not null default now()
);
create index if not exists approvals_date_idx on saa.approvals (trade_date desc);

create table if not exists saa.reconciliations (
  id                 bigserial primary key,
  ts                 timestamptz not null,
  trade_date         date not null,
  run_id             text,
  ok                 boolean not null,
  broker_positions   jsonb not null default '[]'::jsonb,
  local_positions    jsonb not null default '{}'::jsonb,
  live_orders        jsonb not null default '[]'::jsonb,
  mismatches         jsonb not null default '[]'::jsonb,
  open_trades        int,
  error              text,
  created_at         timestamptz not null default now()
);
create index if not exists reconciliations_ts_idx on saa.reconciliations (ts desc);

alter table saa.paper_trades enable row level security;
alter table saa.paper_orders enable row level security;
alter table saa.approvals enable row level security;
alter table saa.reconciliations enable row level security;
grant all on all tables in schema saa to service_role;
grant all on all sequences in schema saa to service_role;

-- ---------------------------------------------------------------------------
-- Write RPCs (public schema, service_role only) — payloads are the daemon's PaperTrade.row() / Ticket.row() /
-- Proposal.row() / reconciliation dicts with trade_date + run_id added by the mirror.
-- ---------------------------------------------------------------------------
create or replace function public.saa_paper_trades_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.paper_trades (engine_key, trade_date, run_id, symbol, option_symbol, occ_symbol, direction, option_type, window_name, lane,
      contracts, account, status, block_reason, proposal_id, decision, decided_at, decision_latency_s, entry_qty, entry_price, entry_at,
      exit_qty, exit_price, exit_at, exit_reason, shadow_entry_bid, shadow_entry_ask, shadow_exit_bid, shadow_r, realized_pnl, realized_r,
      slippage_entry, slippage_exit, fees, sizing_mode, probability, notes, payload, created_at, updated_at)
  select r->>'engine_key', (r->>'trade_date')::date, r->>'run_id', upper(r->>'symbol'), r->>'option_symbol', r->>'occ_symbol', r->>'direction',
         r->>'option_type', r->>'window', r->>'lane', nullif(r->>'contracts','')::int, nullif(r->>'account','')::numeric, coalesce(r->>'status','new'),
         r->>'block_reason', r->>'proposal_id', r->>'decision', nullif(r->>'decided_at','')::timestamptz, nullif(r->>'decision_latency_s','')::numeric,
         nullif(r->>'entry_qty','')::int, nullif(r->>'entry_price','')::numeric, nullif(r->>'entry_at','')::timestamptz,
         nullif(r->>'exit_qty','')::int, nullif(r->>'exit_price','')::numeric, nullif(r->>'exit_at','')::timestamptz, r->>'exit_reason',
         nullif(r->>'shadow_entry_bid','')::numeric, nullif(r->>'shadow_entry_ask','')::numeric, nullif(r->>'shadow_exit_bid','')::numeric,
         nullif(r->>'shadow_r','')::numeric, nullif(r->>'realized_pnl','')::numeric, nullif(r->>'realized_r','')::numeric,
         nullif(r->>'slippage_entry','')::numeric, nullif(r->>'slippage_exit','')::numeric, nullif(r->>'fees','')::numeric, r->>'sizing_mode',
         nullif(r->>'probability','')::numeric, coalesce(r->'notes', '[]'::jsonb), r - 'trade_date' - 'run_id',
         coalesce(nullif(r->>'created_at','')::timestamptz, now()), now()
  from jsonb_array_elements(p_rows) r
  where (r->>'engine_key') is not null and (r->>'symbol') is not null and (r->>'trade_date') is not null
  on conflict (engine_key) do update set
    run_id = excluded.run_id, status = excluded.status, block_reason = excluded.block_reason, proposal_id = excluded.proposal_id,
    decision = excluded.decision, decided_at = excluded.decided_at, decision_latency_s = excluded.decision_latency_s,
    entry_qty = excluded.entry_qty, entry_price = excluded.entry_price, entry_at = excluded.entry_at, exit_qty = excluded.exit_qty,
    exit_price = excluded.exit_price, exit_at = excluded.exit_at, exit_reason = excluded.exit_reason, shadow_exit_bid = excluded.shadow_exit_bid,
    shadow_r = excluded.shadow_r, realized_pnl = excluded.realized_pnl, realized_r = excluded.realized_r, slippage_entry = excluded.slippage_entry,
    slippage_exit = excluded.slippage_exit, fees = excluded.fees, notes = excluded.notes, payload = excluded.payload, updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.saa_paper_orders_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.paper_orders (ticket_id, engine_key, trade_date, run_id, symbol, occ_symbol, side, action, kind, quantity, status, mark_bid, mark_ask,
      limit_prices, broker_order_ids, filled_quantity, avg_fill_price, fills, fees, reason, created_at, done_at, payload, updated_at)
  select r->>'ticket_id', r->>'engine_key', (r->>'trade_date')::date, r->>'run_id', r->>'symbol', r->>'occ_symbol', r->>'side', r->>'action', r->>'kind',
         nullif(r->>'quantity','')::int, coalesce(r->>'status','new'), nullif(r->>'mark_bid','')::numeric, nullif(r->>'mark_ask','')::numeric,
         coalesce(r->'limit_prices', '[]'::jsonb), coalesce(r->'broker_order_ids', '[]'::jsonb), nullif(r->>'filled_quantity','')::int,
         nullif(r->>'avg_fill_price','')::numeric, coalesce(r->'fills', '[]'::jsonb), nullif(r->>'fees','')::numeric, r->>'reason',
         nullif(r->>'created_at','')::timestamptz, nullif(r->>'done_at','')::timestamptz, r - 'trade_date' - 'run_id', now()
  from jsonb_array_elements(p_rows) r
  where (r->>'ticket_id') is not null and (r->>'engine_key') is not null and (r->>'trade_date') is not null
  on conflict (ticket_id) do update set
    status = excluded.status, limit_prices = excluded.limit_prices, broker_order_ids = excluded.broker_order_ids,
    filled_quantity = excluded.filled_quantity, avg_fill_price = excluded.avg_fill_price, fills = excluded.fills, fees = excluded.fees,
    reason = excluded.reason, done_at = excluded.done_at, payload = excluded.payload, updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.saa_approvals_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.approvals (proposal_id, engine_key, trade_date, run_id, symbol, text, sent_at, message_id, timeout_s, status, decided_at, decided_by,
      latency_s, note, payload, updated_at)
  select r->>'proposal_id', r->>'engine_key', (r->>'trade_date')::date, r->>'run_id', upper(r->>'symbol'), r->>'text', nullif(r->>'sent_at','')::timestamptz,
         nullif(r->>'message_id','')::bigint, nullif(r->>'timeout_s','')::numeric, coalesce(r->>'status','pending'), nullif(r->>'decided_at','')::timestamptz,
         r->>'decided_by', nullif(r->>'latency_s','')::numeric, r->>'note', r - 'trade_date' - 'run_id' - 'text', now()
  from jsonb_array_elements(p_rows) r
  where (r->>'proposal_id') is not null and (r->>'engine_key') is not null and (r->>'trade_date') is not null
  on conflict (proposal_id) do update set
    status = excluded.status, message_id = excluded.message_id, decided_at = excluded.decided_at, decided_by = excluded.decided_by,
    latency_s = excluded.latency_s, note = excluded.note, payload = excluded.payload, updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.saa_reconciliations_insert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.reconciliations (ts, trade_date, run_id, ok, broker_positions, local_positions, live_orders, mismatches, open_trades, error)
  select (r->>'ts')::timestamptz, (r->>'trade_date')::date, r->>'run_id', coalesce((r->>'ok')::boolean, false),
         coalesce(r->'broker_positions', '[]'::jsonb), coalesce(r->'local_positions', '{}'::jsonb), coalesce(r->'live_orders', '[]'::jsonb),
         coalesce(r->'mismatches', '[]'::jsonb), nullif(r->>'open_trades','')::int, r->>'error'
  from jsonb_array_elements(p_rows) r
  where (r->>'ts') is not null and (r->>'trade_date') is not null;
  get diagnostics n = row_count;
  return n;
end $$;

-- ---------------------------------------------------------------------------
-- The dashboard read: one document. p_days bounds the ledger / curves; the posterior inputs are the last 200 closed
-- gate-fired engine trades whatever the window (the engine uses 60 for the halt, the prior needs n0 = 30).
-- ---------------------------------------------------------------------------
create or replace function public.saa_dashboard(p_days int default 90) returns jsonb
language plpgsql security definer set search_path = saa, public as $$
declare
  v_since date := saa.et(now())::date - greatest(coalesce(p_days, 90), 1);
  v_doc jsonb;
begin
  select jsonb_build_object(
    'generated_at', now(),
    'window_days', greatest(coalesce(p_days, 90), 1),
    'settings', (select jsonb_object_agg(key, value) from saa.settings
                 where key in ('account_size','kelly_k','halt','daemon_last_seen','index_symbols','engine_cooling_off_after')),
    'goal', jsonb_build_object('start_date', '2026-09-25', 'start_equity', 1000, 'target', 5000000, 'target_date', '2027-09-25'),
    'ledger', (select coalesce(jsonb_agg(row_to_json(l)::jsonb order by l.opened_at desc), '[]'::jsonb) from (
        select s.id, s.engine_key, s.trade_date, s.symbol, s.lane, s.source, s.engine_source, s.notes as window_name, s.direction, s.option_type,
               s.option_symbol, s.strike, s.contracts, s.opened_at, s.exit_at, s.entry_premium, s.exit_premium, s.r_result, s.exit_reason, s.status,
               s.probability_hint, s.mfe_r, s.mae_r, s.model_version,
               case when s.entry_premium is not null and s.r_result is not null then round(s.r_result * s.entry_premium * 100 * coalesce(s.contracts, 1), 2) end as shadow_pnl,
               p.status as paper_status, p.decision, p.decision_latency_s, p.entry_qty, p.entry_price, p.exit_qty, p.exit_price, p.realized_pnl, p.realized_r,
               p.slippage_entry, p.slippage_exit, p.block_reason
        from (select st.*, c.probability as probability_hint from saa.shadow_trades st left join saa.checklists c on c.id = st.checklist_id) s
        left join saa.paper_trades p on p.engine_key = s.engine_key
        where s.trade_date >= v_since
        order by s.opened_at desc limit 300) l),
    'stats', (select coalesce(jsonb_agg(row_to_json(v)::jsonb), '[]'::jsonb) from (
        select source, engine_source, lane,
               count(*) filter (where status = 'closed') as n,
               round(avg(case when r_result > 0 then 1 else 0 end) filter (where status = 'closed'), 3) as hit_rate,
               round(avg(r_result) filter (where status = 'closed' and r_result > 0), 3) as avg_win_r,
               round(avg(r_result) filter (where status = 'closed' and r_result <= 0), 3) as avg_loss_r,
               round(avg(r_result) filter (where status = 'closed'), 3) as expectancy_r,
               round(sum(r_result) filter (where status = 'closed'), 3) as total_r,
               count(*) filter (where status = 'void') as voided
        from saa.shadow_trades where trade_date >= v_since group by source, engine_source, lane) v),
    'r_values', jsonb_build_object(
        'gate', (select coalesce(jsonb_agg(round(r_result, 4) order by exit_at), '[]'::jsonb) from (
                   select r_result, exit_at from saa.shadow_trades
                   where source = 'engine' and engine_source = 'gate' and status = 'closed' and counts_for_rails and r_result is not null
                   order by exit_at desc limit 200) g),
        'fast_lane', (select coalesce(jsonb_agg(round(r_result, 4) order by exit_at), '[]'::jsonb) from (
                   select r_result, exit_at from saa.shadow_trades
                   where source = 'engine' and engine_source = 'fast_lane' and status = 'closed' and r_result is not null
                   order by exit_at desc limit 300) f),
        'modeled', (select coalesce(jsonb_agg(round(r_result, 4) order by exit_at), '[]'::jsonb) from (
                   select r_result, exit_at from saa.shadow_trades
                   where source in ('brief','tv','manual') and status = 'closed' and r_result is not null
                   order by exit_at desc limit 300) m),
        'paper', (select coalesce(jsonb_agg(round(realized_r, 4) order by exit_at), '[]'::jsonb) from (
                   select realized_r, exit_at from saa.paper_trades where status = 'closed' and realized_r is not null
                   order by exit_at desc limit 300) p)),
    'brier', (select coalesce(jsonb_agg(row_to_json(b)::jsonb), '[]'::jsonb) from saa.v_brier b),
    'calibration_n', (select count(*) from saa.calibration),
    'paper', jsonb_build_object(
        'trades', (select count(*) from saa.paper_trades where trade_date >= v_since),
        'closed', (select count(*) from saa.paper_trades where status = 'closed'),
        'open', (select count(*) from saa.paper_trades where status in ('working','open','closing')),
        'realized_pnl', (select coalesce(round(sum(realized_pnl), 2), 0) from saa.paper_trades where status = 'closed'),
        'realized_r', (select coalesce(round(sum(realized_r), 4), 0) from saa.paper_trades where status = 'closed'),
        'by_status', (select coalesce(jsonb_object_agg(status, n), '{}'::jsonb) from (select status, count(*) n from saa.paper_trades group by status) x),
        'approvals', (select coalesce(jsonb_object_agg(status, n), '{}'::jsonb) from (select status, count(*) n from saa.approvals group by status) x),
        'avg_decision_latency_s', (select round(avg(latency_s), 1) from saa.approvals where status in ('approved','skipped')),
        'reconciliations', jsonb_build_object(
            'n', (select count(*) from saa.reconciliations where ts >= now() - interval '7 days'),
            'mismatches', (select count(*) from saa.reconciliations where not ok and ts >= now() - interval '7 days'),
            'last', (select to_jsonb(r) from (select ts, ok, mismatches, open_trades, error from saa.reconciliations order by ts desc limit 1) r)),
        'equity_curve', (select coalesce(jsonb_agg(jsonb_build_object('date', d, 'pnl', pnl, 'cum', cum) order by d), '[]'::jsonb) from (
            select trade_date as d, round(sum(realized_pnl), 2) as pnl, round(sum(sum(realized_pnl)) over (order by trade_date), 2) as cum
            from saa.paper_trades where status = 'closed' and realized_pnl is not null group by trade_date) e),
        'recent', (select coalesce(jsonb_agg(row_to_json(t)::jsonb order by t.created_at desc), '[]'::jsonb) from (
            select engine_key, trade_date, symbol, option_symbol, status, block_reason, decision, decision_latency_s, entry_qty, entry_price, exit_qty, exit_price,
                   exit_reason, realized_pnl, realized_r, shadow_r, slippage_entry, slippage_exit, created_at
            from saa.paper_trades order by created_at desc limit 50) t)),
    'daily_records', (select coalesce(jsonb_agg(row_to_json(d)::jsonb), '[]'::jsonb) from (select * from saa.v_daily_records limit 30) d),
    'daemon', (select to_jsonb(r) from (select run_id, trade_date, started_at, ended_at, last_seen, status, host, version,
                                           stats->'feed' as feed, stats->'engine'->'counts' as engine_counts, stats->'engine'->'feed_mode' as engine_feed_mode,
                                           stats->'execution' as execution
                                    from saa.daemon_runs order by started_at desc limit 1) r),
    'today', jsonb_build_object(
        'date', saa.et(now())::date,
        'decisions', (select coalesce(jsonb_object_agg(decision, n), '{}'::jsonb) from (
                        select decision, count(*) n from saa.engine_decisions where trade_date = saa.et(now())::date group by decision) x),
        'stand_downs', (select coalesce(jsonb_object_agg(k, n), '{}'::jsonb) from (
                        select split_part(reason, ':', 1) as k, count(*) n from saa.engine_decisions
                        where trade_date = saa.et(now())::date and decision = 'stand_down' group by 1 order by 2 desc limit 8) x),
        'checklists', (select count(*) from saa.checklists where trade_date = saa.et(now())::date))
  ) into v_doc;
  return v_doc;
end $$;

-- Lock the new RPCs down to the service role (same pattern as 0001/0002/0005/0007).
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname in ('saa_paper_trades_upsert','saa_paper_orders_upsert','saa_approvals_upsert',
                                                        'saa_reconciliations_insert','saa_dashboard') loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    execute format('grant execute on function %s to service_role', f.sig);
  end loop;
end $$;
