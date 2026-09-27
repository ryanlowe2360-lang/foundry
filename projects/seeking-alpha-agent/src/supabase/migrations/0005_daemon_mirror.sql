-- 0005_daemon_mirror.sql — M2 daemon data plane (applied 2026-09-27).
-- Tables the Python daemon (src/daemon/) mirrors its SQLite hot state into, plus the
-- service-role-only RPC surface it calls through PostgREST (/rest/v1/rpc/saa_*).
-- Re-runnable.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

-- 1-minute bars from DXLink Candle events (regular hours). The daemon upserts the forming bar
-- every few seconds and marks it complete when the next minute starts.
create table if not exists saa.bars_1m (
  symbol       text not null,
  bar_time     timestamptz not null,            -- bar start (UTC)
  open         numeric,
  high         numeric,
  low          numeric,
  close        numeric,
  volume       numeric,
  vwap         numeric,
  trade_count  int,
  complete     boolean not null default false,
  source       text not null default 'dxlink',
  updated_at   timestamptz not null default now(),
  primary key (symbol, bar_time)
);
create index if not exists bars_1m_time_idx on saa.bars_1m (bar_time desc);

-- Option-chain snapshots every 5 minutes. Strikes are compact arrays to keep rows small:
-- expirations = [{"exp":"2026-09-28","dte":0,"strikes":[[strike, call_bid, call_ask, call_iv, call_delta,
--   call_gamma, call_oi, call_volume, put_bid, put_ask, put_iv, put_delta, put_gamma, put_oi, put_volume], ...]}]
create table if not exists saa.chain_snapshots (
  id          bigserial primary key,
  ts          timestamptz not null,
  underlying  text not null,
  spot        numeric,
  expirations jsonb not null,
  summary     jsonb not null default '{}'::jsonb,  -- {atm_iv, call_oi, put_oi, pc_oi, call_vol, put_vol, n_strikes, n_exp}
  gamma       jsonb,                                -- {net_gex, call_gex, put_gex, flip, call_wall, put_wall, regime, coverage}
  source      text not null default 'dxlink'
);
create index if not exists chain_snapshots_und_ts_idx on saa.chain_snapshots (underlying, ts desc);
create index if not exists chain_snapshots_ts_idx on saa.chain_snapshots (ts desc);

-- Trading halts (Nasdaq Trader RSS + DXLink Profile events).
create table if not exists saa.halts (
  symbol                text not null,
  halt_time             timestamptz not null,
  reason_code           text,
  market                text,
  resumption_time       timestamptz,
  source                text not null default 'nasdaq_rss',
  raw                   jsonb,
  updated_at            timestamptz not null default now(),
  primary key (symbol, halt_time)
);

-- One row per daemon run (live-updated stats; what the dashboard and the M5 missing-heartbeat
-- alert read). saa.run_log still gets the start / heartbeat / end entries.
create table if not exists saa.daemon_runs (
  id          bigserial primary key,
  run_id      text not null unique,
  trade_date  date not null,
  started_at  timestamptz not null default now(),
  ended_at    timestamptz,
  last_seen   timestamptz not null default now(),
  host        text,
  version     text,
  mode        text,                                 -- session | smoke | forever | replay
  status      text not null default 'running',     -- running | done | failed
  stats       jsonb not null default '{}'::jsonb,
  errors      jsonb not null default '[]'::jsonb
);
create index if not exists daemon_runs_date_idx on saa.daemon_runs (trade_date desc);

-- RLS on (service role only), same posture as every other saa.* table.
do $$
declare t text;
begin
  for t in select unnest(array['bars_1m','chain_snapshots','halts','daemon_runs']) loop
    execute format('alter table saa.%I enable row level security', t);
  end loop;
end $$;
grant all on all tables in schema saa to service_role;
grant all on all sequences in schema saa to service_role;

-- ---------------------------------------------------------------------------
-- RPC surface for the daemon (public schema, service_role only)
-- ---------------------------------------------------------------------------

-- Bulk upsert of bars. Rows: [{symbol, bar_time, open, high, low, close, volume, vwap, trade_count, complete}]
create or replace function public.saa_bars_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.bars_1m (symbol, bar_time, open, high, low, close, volume, vwap, trade_count, complete, source, updated_at)
  select upper(r->>'symbol'), (r->>'bar_time')::timestamptz,
         nullif(r->>'open','')::numeric, nullif(r->>'high','')::numeric, nullif(r->>'low','')::numeric, nullif(r->>'close','')::numeric,
         nullif(r->>'volume','')::numeric, nullif(r->>'vwap','')::numeric, nullif(r->>'trade_count','')::int,
         coalesce((r->>'complete')::boolean, false), coalesce(nullif(r->>'source',''), 'dxlink'), now()
  from jsonb_array_elements(p_rows) r
  where (r->>'symbol') is not null and (r->>'bar_time') is not null
  on conflict (symbol, bar_time) do update set
    open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
    volume = excluded.volume, vwap = excluded.vwap, trade_count = excluded.trade_count,
    complete = saa.bars_1m.complete or excluded.complete, updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.saa_chain_snapshot(p_ts timestamptz, p_underlying text, p_spot numeric,
                                                     p_expirations jsonb, p_summary jsonb default '{}'::jsonb,
                                                     p_gamma jsonb default null, p_source text default 'dxlink') returns bigint
language sql security definer set search_path = saa, public as $$
  insert into saa.chain_snapshots (ts, underlying, spot, expirations, summary, gamma, source)
  values (p_ts, upper(p_underlying), p_spot, p_expirations, coalesce(p_summary, '{}'::jsonb), p_gamma, coalesce(p_source, 'dxlink'))
  returning id $$;

-- Rows: [{symbol, halt_time, reason_code, market, resumption_time, source, raw}]
create or replace function public.saa_halts_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.halts (symbol, halt_time, reason_code, market, resumption_time, source, raw, updated_at)
  select upper(r->>'symbol'), (r->>'halt_time')::timestamptz, r->>'reason_code', r->>'market',
         nullif(r->>'resumption_time','')::timestamptz, coalesce(nullif(r->>'source',''), 'nasdaq_rss'), r->'raw', now()
  from jsonb_array_elements(p_rows) r
  where (r->>'symbol') is not null and (r->>'halt_time') is not null
  on conflict (symbol, halt_time) do update set
    reason_code = coalesce(excluded.reason_code, saa.halts.reason_code),
    resumption_time = coalesce(excluded.resumption_time, saa.halts.resumption_time),
    raw = coalesce(excluded.raw, saa.halts.raw), updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

-- Upsert the run row; p_patch may carry trade_date, started_at, ended_at, host, version, mode, status, stats, errors.
-- Every call also bumps last_seen and saa.settings.daemon_last_seen (the machine heartbeat).
create or replace function public.saa_daemon_run(p_run_id text, p_patch jsonb default '{}'::jsonb) returns void
language plpgsql security definer set search_path = saa, public as $$
begin
  insert into saa.daemon_runs (run_id, trade_date, started_at, host, version, mode, status, stats, errors, last_seen)
  values (p_run_id,
          coalesce(nullif(p_patch->>'trade_date','')::date, saa.et(now())::date),
          coalesce(nullif(p_patch->>'started_at','')::timestamptz, now()),
          p_patch->>'host', p_patch->>'version', coalesce(p_patch->>'mode', 'session'),
          coalesce(p_patch->>'status', 'running'), coalesce(p_patch->'stats', '{}'::jsonb), coalesce(p_patch->'errors', '[]'::jsonb), now())
  on conflict (run_id) do update set
    ended_at = coalesce(nullif(p_patch->>'ended_at','')::timestamptz, saa.daemon_runs.ended_at),
    status   = coalesce(p_patch->>'status', saa.daemon_runs.status),
    stats    = case when p_patch ? 'stats' then p_patch->'stats' else saa.daemon_runs.stats end,
    errors   = case when p_patch ? 'errors' then p_patch->'errors' else saa.daemon_runs.errors end,
    host     = coalesce(p_patch->>'host', saa.daemon_runs.host),
    version  = coalesce(p_patch->>'version', saa.daemon_runs.version),
    last_seen = now();
  insert into saa.settings (key, value) values ('daemon_last_seen', now()::text)
  on conflict (key) do update set value = excluded.value, updated_at = now();
end $$;

-- The day's calendar row (watch list, earnings, econ, vix, index quotes) as one JSON object.
create or replace function public.saa_calendar_day(p_date date default null) returns jsonb
language sql security definer set search_path = saa, public as $$
  select coalesce((select to_jsonb(d) from saa.calendar_days d where d.trade_date = coalesce(p_date, saa.et(now())::date)), '{}'::jsonb) $$;

-- Hand-maintained economic calendar (FOMC / CPI / NFP …). Rows: [{date, time_et, event, source}].
-- Merges into calendar_days.econ for each date (deduped on event name), creating the day row if needed.
create or replace function public.saa_econ_upsert(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare r jsonb; d date; n int := 0; existing jsonb; ev jsonb;
begin
  for r in select * from jsonb_array_elements(p_rows) loop
    d := (r->>'date')::date;
    ev := jsonb_build_object('event', r->>'event', 'time_et', r->>'time_et', 'source', coalesce(r->>'source', 'hand'), 'kind', coalesce(r->>'kind', 'macro'));
    insert into saa.calendar_days (trade_date) values (d) on conflict (trade_date) do nothing;
    select econ into existing from saa.calendar_days where trade_date = d;
    if not exists (select 1 from jsonb_array_elements(existing) e where e->>'event' = r->>'event') then
      update saa.calendar_days set econ = existing || jsonb_build_array(ev), updated_at = now() where trade_date = d;
      n := n + 1;
    end if;
  end loop;
  return n;
end $$;

-- Small read helpers for the smoke test / dashboard.
create or replace function public.saa_daemon_status() returns jsonb
language sql security definer set search_path = saa, public as $$
  select jsonb_build_object(
    'last_seen', (select value from saa.settings where key = 'daemon_last_seen'),
    'last_run',  (select to_jsonb(r) from saa.daemon_runs r order by started_at desc limit 1),
    'bars_today', (select count(*) from saa.bars_1m where bar_time >= saa.et_to_ts(saa.et(now())::date + time '00:00')),
    'snapshots_today', (select count(*) from saa.chain_snapshots where ts >= saa.et_to_ts(saa.et(now())::date + time '00:00'))) $$;

-- Lock the new RPCs down to the service role (same pattern as 0001/0002).
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname in ('saa_bars_upsert','saa_chain_snapshot','saa_halts_upsert','saa_daemon_run',
                                                        'saa_calendar_day','saa_econ_upsert','saa_daemon_status') loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    execute format('grant execute on function %s to service_role', f.sig);
  end loop;
end $$;

-- Retention: extend the daily housekeeping job (bars 120d, chain snapshots 45d, halts 180d, runs 180d).
do $$ begin
  perform cron.unschedule('saa_housekeeping');
exception when others then null; end $$;
select cron.schedule('saa_housekeeping', '0 6 * * *', $$
  delete from saa.price_ticks where ts < now() - interval '90 days';
  delete from saa.run_log where ts < now() - interval '60 days';
  delete from saa.market_snapshots where ts < now() - interval '180 days';
  delete from saa.bars_1m where bar_time < now() - interval '120 days';
  delete from saa.chain_snapshots where ts < now() - interval '45 days';
  delete from saa.halts where halt_time < now() - interval '180 days';
  delete from saa.daemon_runs where started_at < now() - interval '180 days'$$);
