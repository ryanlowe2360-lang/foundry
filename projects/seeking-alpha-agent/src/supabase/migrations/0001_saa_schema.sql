-- 0001_saa_schema.sql — Seeking Alpha Agent, milestone M1 (Proving Ground)
-- Project: Quant edge (zspbkcheounkwnpjkgrv). Everything lives in schema `saa`;
-- the existing public.* tables of the earlier system are untouched.
--
-- Design (DECISIONS D4/D5):
--   * schema `saa` is NOT exposed through PostgREST. Edge functions reach it only
--     through SECURITY DEFINER RPCs in `public` named saa_* (service_role only).
--   * pg_cron + pg_net call the edge functions from inside Postgres; an insert
--     trigger on saa.outbox delivers Telegram messages immediately.
--   * The shadow-trade option model (Black-Scholes, r = 0, trading-time clock) is
--     implemented here in plpgsql so it is deterministic and unit-testable in-DB.

create extension if not exists pg_net with schema extensions;
create extension if not exists pg_cron;
create extension if not exists pgcrypto with schema extensions;

create schema if not exists saa;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table if not exists saa.settings (
  key         text primary key,
  value       text not null,
  updated_at  timestamptz not null default now()
);

create table if not exists saa.rules (
  id             bigserial primary key,
  version        int not null unique,
  effective_from date not null default current_date,
  params         jsonb not null,
  evidence       text,
  created_by     text not null default 'intake',
  created_at     timestamptz not null default now()
);

create table if not exists saa.symbols (
  symbol      text primary key,
  kind        text not null check (kind in ('index','single')),
  daily_vol   numeric not null default 0.025,   -- σ_d as a fraction (2.5%/day)
  strike_step numeric not null default 1.0,
  spread_frac numeric not null default 0.05,    -- modeled bid-ask as fraction of premium
  active      boolean not null default true,
  notes       text,
  updated_at  timestamptz not null default now()
);

create table if not exists saa.calendar_days (
  trade_date   date primary key,
  earnings     jsonb not null default '[]'::jsonb,
  ipos         jsonb not null default '[]'::jsonb,
  econ         jsonb not null default '[]'::jsonb,
  vix          jsonb not null default '{}'::jsonb,   -- {vix, vix1d, vix9d, vix3m, fetched_at}
  index_quotes jsonb not null default '{}'::jsonb,   -- {SPY:{c,o,h,l,pc,t}, ...}
  watch        jsonb not null default '[]'::jsonb,   -- symbols the brief put in play
  errors       jsonb not null default '[]'::jsonb,
  fetched_at   timestamptz,
  updated_at   timestamptz not null default now()
);

create table if not exists saa.market_snapshots (
  id       bigserial primary key,
  ts       timestamptz not null default now(),
  kind     text not null,            -- 'vix_term' | 'index_quote' | 'open_snapshot'
  payload  jsonb not null
);
create index if not exists market_snapshots_ts_idx on saa.market_snapshots (ts desc);

create table if not exists saa.price_ticks (
  symbol     text not null,
  ts         timestamptz not null,
  price      numeric not null,
  day_open   numeric,
  day_high   numeric,
  day_low    numeric,
  prev_close numeric,
  primary key (symbol, ts)
);
create index if not exists price_ticks_ts_idx on saa.price_ticks (ts);

create table if not exists saa.triggers (
  id              bigserial primary key,
  received_at     timestamptz not null default now(),
  trade_date      date not null,
  symbol          text not null,
  lane            text not null,        -- orb | vwap | continuation | rvol | compression | other
  direction       text not null default 'none' check (direction in ('long','short','none')),
  price           numeric,
  bar_time        timestamptz,
  payload         jsonb not null,
  source_ip       text,
  dedupe_key      text unique,
  shadow_trade_id bigint,
  status          text not null default 'received'   -- received | shadow_opened | voided | flag_only
);
create index if not exists triggers_trade_date_idx on saa.triggers (trade_date, symbol);

create table if not exists saa.briefs (
  id            bigserial primary key,
  trade_date    date not null unique,
  generated_at  timestamptz not null default now(),
  regime        jsonb not null default '{}'::jsonb,
  candidates    jsonb not null default '[]'::jsonb,
  stand_down    boolean not null default true,
  text          text not null,
  raw           jsonb,
  model         text,
  run_id        text
);

create table if not exists saa.checklists (
  id             bigserial primary key,
  trade_date     date not null,
  symbol         text not null,
  brief_id       bigint references saa.briefs(id),
  created_at     timestamptz not null default now(),
  window_kind    text not null,                  -- open | event | last_hour | fast_lane | day2
  window_start   timestamptz,
  window_end     timestamptz,
  catalyst       text,
  catalyst_class text,
  gates          jsonb not null default '{}'::jsonb,   -- {catalyst, readable, favorable, wedge, direction, trigger} + notes
  gates_passed   int not null default 0,
  arming_items   jsonb not null default '{}'::jsonb,   -- {"1":{score,weight,note},...}
  arming_score   int not null default 0,
  arming_max     int not null default 56,
  direction      text not null default 'none' check (direction in ('long','short','none','two_sided')),
  structure      text,                            -- e.g. 'long call 0DTE 0.5σ OTM'
  thesis         text,
  invalidation   text,
  target_r       numeric,
  probability    numeric check (probability >= 0 and probability <= 1),  -- P(shadow result > 0)
  size_r         numeric not null default 0,      -- M1: always 0 (journal only)
  decision       text not null default 'stand_down',   -- stand_down | shadow | fire
  shadow_trade_id bigint,
  outcome_r      numeric,
  brier          numeric,
  status         text not null default 'open'     -- open | resolved | void
);
create index if not exists checklists_trade_date_idx on saa.checklists (trade_date);

create table if not exists saa.shadow_trades (
  id               bigserial primary key,
  trade_date       date not null,
  symbol           text not null,
  lane             text not null,
  direction        text not null check (direction in ('long','short')),
  source           text not null default 'tv',   -- tv | brief | manual
  checklist_id     bigint references saa.checklists(id),
  trigger_id       bigint references saa.triggers(id),
  opened_at        timestamptz not null default now(),
  window_end       timestamptz not null,
  expiry           timestamptz not null,
  option_type      text not null check (option_type in ('call','put')),
  strike           numeric not null,
  sigma_d          numeric not null,
  entry_underlying numeric not null,
  entry_mid        numeric not null,     -- modeled option mid at entry (per share)
  entry_premium    numeric not null,     -- modeled ask = mid + spread/2 (per share); R = this × 100
  spread_frac      numeric not null,
  status           text not null default 'open' check (status in ('open','closed','void')),
  exit_at          timestamptz,
  exit_underlying  numeric,
  exit_premium     numeric,             -- modeled bid at exit
  exit_reason      text,                -- time_stop | trail | expiry | void_no_data | void_window
  r_result         numeric,
  mfe_r            numeric,
  mae_r            numeric,
  hwm_premium      numeric,
  trail_activated  boolean not null default false,
  ticks_used       int,
  model_version    text not null default 'bs-r0-tradingclock-v1',
  notes            text
);
create index if not exists shadow_trades_status_idx on saa.shadow_trades (status, window_end);
create index if not exists shadow_trades_trade_date_idx on saa.shadow_trades (trade_date);

alter table saa.triggers
  add constraint triggers_shadow_fk foreign key (shadow_trade_id) references saa.shadow_trades(id);
alter table saa.checklists
  add constraint checklists_shadow_fk foreign key (shadow_trade_id) references saa.shadow_trades(id);

create table if not exists saa.calibration (
  id            bigserial primary key,
  trade_date    date not null,
  checklist_id  bigint not null references saa.checklists(id) unique,
  probability   numeric not null,
  outcome       int not null check (outcome in (0,1)),
  brier         numeric not null,
  bucket        text not null,
  created_at    timestamptz not null default now()
);

create table if not exists saa.reviews (
  id              bigserial primary key,
  week_ending     date not null unique,
  generated_at    timestamptz not null default now(),
  stats           jsonb not null default '{}'::jsonb,
  text            text not null,
  proposed_change jsonb,
  status          text not null default 'draft'    -- draft | approved | rejected
);

create table if not exists saa.outbox (
  id                  bigserial primary key,
  created_at          timestamptz not null default now(),
  kind                text not null default 'system',   -- brief | tally | review | alert | system | test
  text                text not null,
  parse_mode          text,                              -- null (plain) | 'HTML' | 'MarkdownV2'
  status              text not null default 'pending' check (status in ('pending','sent','failed')),
  attempts            int not null default 0,
  sent_at             timestamptz,
  error               text,
  telegram_message_id bigint
);
create index if not exists outbox_status_idx on saa.outbox (status, created_at);

create table if not exists saa.run_log (
  id      bigserial primary key,
  ts      timestamptz not null default now(),
  job     text not null,
  ok      boolean not null,
  detail  jsonb
);
create index if not exists run_log_ts_idx on saa.run_log (ts desc);

-- RLS on everything; no policies → only the service role / postgres can read or write.
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'saa' loop
    execute format('alter table saa.%I enable row level security', t);
  end loop;
end $$;

grant usage on schema saa to service_role;
grant all on all tables in schema saa to service_role;
grant all on all sequences in schema saa to service_role;
alter default privileges in schema saa grant all on tables to service_role;
alter default privileges in schema saa grant all on sequences to service_role;
revoke all on schema saa from anon, authenticated;

-- ---------------------------------------------------------------------------
-- Seed: settings, symbols, Tier 2 rules v1 (corpus placeholders, to be fit to the log)
-- ---------------------------------------------------------------------------

insert into saa.settings (key, value) values
  ('account_size', '1000'),
  ('kelly_k', '0.5'),
  ('internal_key', encode(extensions.gen_random_bytes(24), 'hex')),
  ('tv_webhook_secret', encode(extensions.gen_random_bytes(18), 'hex')),
  ('tv_ip_check', 'true'),
  ('tv_ip_allowlist', '52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7'),
  ('telegram_chat_id', ''),
  ('index_symbols', 'SPY,QQQ,IWM'),
  ('quotes_window_et', '09:28-16:02'),
  ('objective', '$1,000 -> $5,000,000 by 2027-09-25'),
  ('objective_start_date', '2026-09-28'),
  ('objective_end_date', '2027-09-25')
on conflict (key) do nothing;

insert into saa.symbols (symbol, kind, daily_vol, strike_step, spread_frac, notes) values
  ('SPY', 'index', 0.010, 1.0, 0.03, 'σ_d replaced by VIX1D/100/√252 at runtime when available'),
  ('QQQ', 'index', 0.012, 1.0, 0.03, 'σ_d scaled 1.2× VIX1D proxy at runtime'),
  ('IWM', 'index', 0.013, 1.0, 0.05, 'σ_d scaled 1.3× VIX1D proxy at runtime')
on conflict (symbol) do nothing;

insert into saa.rules (version, params, evidence, created_by) values (1, jsonb_build_object(
  'arming_thresholds', jsonb_build_object('partial', 15, 'armed', 28, 'max', 56),
  'partial_bank_frac_of_account', 0.075,
  'trail', jsonb_build_object('frac', 0.30, 'tight_frac', 0.20, 'tight_at_r', 3.0, 'atr_ticks', 10),
  'spread_filter', jsonb_build_object('flag', 0.05, 'skip', 0.10),
  'daily_stop_r', -3, 'consecutive_loss_lockout', 3,
  'cooling_off', jsonb_build_object('win_r', 5, 'day_r', 8, 'next_size_mult', 0.5),
  'no_entry_et', jsonb_build_array('11:30', '13:30'),
  'windows_et', jsonb_build_array('09:30-10:00', '10:00-11:30', '13:30-15:00', '15:00-15:55'),
  'shadow', jsonb_build_object('strike_sigma_otm', 0.5, 'contracts', 1, 'min_spread_abs', 0.02,
                               'single_name_default_sigma_d', 0.025, 'last_entry_et', '15:30'),
  'fast_lanes', jsonb_build_object(
      'orb',          jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '10:30'),
      'vwap',         jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '15:30'),
      'continuation', jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '11:30'),
      'rvol',         jsonb_build_object('enabled', true,  'opens_shadow', true,  'entry_until_et', '11:30'),
      'compression',  jsonb_build_object('enabled', true,  'opens_shadow', false)),
  'promotion_gate', jsonb_build_object('min_shadow_trades', 60, 'expectancy_gt', 0),
  'tier2_change_policy', jsonb_build_object('min_relevant_trades', 30, 'max_changes_per_week', 1)
), 'Corpus placeholders (explosive_day_checklist_modeB §9; Process Discipline; build plan §3–5). Fit to the log.', 'intake')
on conflict (version) do nothing;

-- ---------------------------------------------------------------------------
-- Time helpers (America/New_York)
-- ---------------------------------------------------------------------------

create or replace function saa.et(ts timestamptz) returns timestamp
language sql stable as $$ select ts at time zone 'America/New_York' $$;

create or replace function saa.et_to_ts(local_ts timestamp) returns timestamptz
language sql stable as $$ select local_ts at time zone 'America/New_York' $$;

-- Next expiry for a symbol at time `ts`: index → same session 16:00 ET (or next weekday if after close);
-- single name → the coming Friday 16:00 ET (today if Friday and before close).
create or replace function saa.next_expiry(sym text, ts timestamptz) returns timestamptz
language plpgsql stable as $$
declare
  k text; d date; local_ts timestamp; dow int;
begin
  select kind into k from saa.symbols where symbol = sym;
  if k is null then k := 'single'; end if;
  local_ts := saa.et(ts);
  d := local_ts::date;
  if local_ts::time >= time '16:00' then d := d + 1; end if;
  dow := extract(isodow from d);           -- 1=Mon .. 7=Sun
  if k = 'index' then
    while extract(isodow from d) > 5 loop d := d + 1; end loop;
    return saa.et_to_ts(d + time '16:00');
  else
    if dow > 5 then d := d + (8 - dow); dow := 1; end if;   -- roll weekend to Monday
    d := d + (5 - dow);                                      -- to Friday
    return saa.et_to_ts(d + time '16:00');
  end if;
end $$;

-- Trading-time fraction of a year between ts and expiry: whole business days + today's remaining session.
create or replace function saa.t_years(ts timestamptz, expiry timestamptz) returns numeric
language plpgsql stable as $$
declare
  a timestamp := saa.et(ts); b timestamp := saa.et(expiry);
  secs numeric; days int := 0; d date;
begin
  if ts >= expiry then return 0; end if;
  if a::date = b::date then
    secs := greatest(0, extract(epoch from (least(b::time, time '16:00') - greatest(a::time, time '09:30'))));
    return (secs / 23400.0) / 252.0;
  end if;
  -- remaining part of today's session
  secs := greatest(0, extract(epoch from (time '16:00' - greatest(a::time, time '09:30'))));
  d := a::date + 1;
  while d < b::date loop
    if extract(isodow from d) <= 5 then days := days + 1; end if;
    d := d + 1;
  end loop;
  -- expiry day session up to 16:00
  secs := secs + greatest(0, extract(epoch from (least(b::time, time '16:00') - time '09:30')));
  return (days + secs / 23400.0) / 252.0;
end $$;

-- ---------------------------------------------------------------------------
-- Option model: Black-Scholes, r = 0, σ_annual = σ_d·√252
-- ---------------------------------------------------------------------------

create or replace function saa.norm_cdf(x double precision) returns double precision
language plpgsql immutable as $$
declare ax double precision; t double precision; y double precision;
begin
  -- Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7)
  ax := abs(x) / sqrt(2.0);
  t := 1.0 / (1.0 + 0.3275911 * ax);
  y := 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * exp(-ax * ax);
  return case when x >= 0 then 0.5 * (1.0 + y) else 0.5 * (1.0 - y) end;
end $$;

create or replace function saa.bs_price(s numeric, k numeric, sigma_d numeric, t_years numeric, opt_type text)
returns numeric language plpgsql immutable as $$
declare sig double precision; sqt double precision; d1 double precision; d2 double precision; px double precision;
begin
  if s <= 0 or k <= 0 then return 0; end if;
  if t_years <= 0 or sigma_d <= 0 then
    return case when opt_type = 'call' then greatest(s - k, 0) else greatest(k - s, 0) end;
  end if;
  sig := sigma_d * sqrt(252.0);
  sqt := sqrt(t_years);
  d1 := (ln(s / k) + 0.5 * sig * sig * t_years) / (sig * sqt);
  d2 := d1 - sig * sqt;
  if opt_type = 'call' then
    px := s * saa.norm_cdf(d1) - k * saa.norm_cdf(d2);
  else
    px := k * saa.norm_cdf(-d2) - s * saa.norm_cdf(-d1);
  end if;
  return round(greatest(px, 0)::numeric, 4);
end $$;

-- σ_d for a symbol on a date: index symbols use VIX1D (fallback VIX) from calendar_days; singles use symbols.daily_vol.
create or replace function saa.sigma_d_for(sym text, d date) returns numeric
language plpgsql stable as $$
declare k text; dv numeric; v jsonb; base numeric; mult numeric := 1.0;
begin
  select kind, daily_vol into k, dv from saa.symbols where symbol = sym;
  if k = 'index' then
    select vix into v from saa.calendar_days where trade_date = d;
    base := coalesce((v->>'vix1d')::numeric, (v->>'vix')::numeric);
    if base is not null and base > 0 then
      mult := case sym when 'QQQ' then 1.2 when 'IWM' then 1.3 else 1.0 end;
      return round((base / 100.0) / sqrt(252.0) * mult, 5);
    end if;
    return dv;
  end if;
  return coalesce(dv, ((select params->'shadow'->>'single_name_default_sigma_d' from saa.rules order by version desc limit 1))::numeric, 0.025);
end $$;

-- Window edge (time stop) for an entry at ts. Returns null when no entry is allowed.
create or replace function saa.window_end_for(ts timestamptz) returns timestamptz
language plpgsql stable as $$
declare l timestamp := saa.et(ts); t time := l::time; d date := l::date;
begin
  if t < time '09:30' then return null; end if;
  if t < time '10:00' then return saa.et_to_ts(d + time '10:00'); end if;
  if t < time '11:30' then return saa.et_to_ts(d + time '11:30'); end if;
  if t < time '13:30' then return null; end if;            -- Tier 1: no fresh entries 11:30–13:30
  if t < time '15:00' then return saa.et_to_ts(d + time '15:00'); end if;
  if t < time '15:30' then return saa.et_to_ts(d + time '15:55'); end if;   -- last entry 15:30 in M1
  return null;
end $$;

-- ---------------------------------------------------------------------------
-- Shadow trade open / score
-- ---------------------------------------------------------------------------

create or replace function saa.open_shadow_trade(
  p_symbol text, p_lane text, p_direction text, p_underlying numeric, p_at timestamptz,
  p_source text default 'tv', p_trigger_id bigint default null, p_checklist_id bigint default null,
  p_window_end timestamptz default null
) returns bigint language plpgsql as $$
declare
  v_id bigint; v_date date := saa.et(p_at)::date; v_end timestamptz; v_exp timestamptz;
  v_sig numeric; v_otm numeric; v_step numeric; v_spread numeric; v_min_spread numeric;
  v_type text; v_k numeric; v_mid numeric; v_ask numeric; v_rules jsonb;
begin
  select params into v_rules from saa.rules order by version desc limit 1;
  v_end := coalesce(p_window_end, saa.window_end_for(p_at));
  if v_end is null then return null; end if;                       -- outside an entry window
  v_exp := saa.next_expiry(p_symbol, p_at);
  if v_end > v_exp then v_end := v_exp; end if;
  v_sig := saa.sigma_d_for(p_symbol, v_date);
  v_otm := coalesce((v_rules->'shadow'->>'strike_sigma_otm')::numeric, 0.5);
  v_min_spread := coalesce((v_rules->'shadow'->>'min_spread_abs')::numeric, 0.02);
  select coalesce(strike_step, 1.0), coalesce(spread_frac, 0.05) into v_step, v_spread from saa.symbols where symbol = p_symbol;
  if v_step is null then
    v_step := case when p_underlying < 25 then 0.5 when p_underlying < 200 then 1.0 else 5.0 end;
    v_spread := 0.08;
  end if;
  v_type := case when p_direction = 'long' then 'call' else 'put' end;
  v_k := round((p_underlying * (1 + (case when p_direction = 'long' then 1 else -1 end) * v_otm * v_sig)) / v_step) * v_step;
  v_mid := saa.bs_price(p_underlying, v_k, v_sig, saa.t_years(p_at, v_exp), v_type);
  if v_mid <= 0 then return null; end if;
  v_ask := round(v_mid + greatest(v_mid * v_spread, v_min_spread) / 2, 4);
  insert into saa.shadow_trades (trade_date, symbol, lane, direction, source, checklist_id, trigger_id, opened_at,
      window_end, expiry, option_type, strike, sigma_d, entry_underlying, entry_mid, entry_premium, spread_frac)
  values (v_date, p_symbol, p_lane, p_direction, p_source, p_checklist_id, p_trigger_id, p_at,
      v_end, v_exp, v_type, v_k, v_sig, p_underlying, v_mid, v_ask, v_spread)
  returning id into v_id;
  return v_id;
end $$;

-- Walk the tick path of one open shadow trade; close it if a time stop / trail / expiry is hit.
-- Returns the exit reason or null when the trade stays open.
create or replace function saa.score_shadow_trade(p_id bigint, p_now timestamptz default now())
returns text language plpgsql as $$
declare
  st saa.shadow_trades%rowtype; r record; v_rules jsonb;
  bank_frac numeric; account numeric; activation_r numeric;
  trail_frac numeric; tight_frac numeric; tight_at numeric; atr_n int;
  entry_ask numeric; spread_abs numeric; prem_mid numeric; prem_bid numeric;
  hwm numeric; gain_r numeric; mfe numeric := 0; mae numeric := 0;
  trail_on boolean := false; stop_px numeric; atr numeric;
  last_ts timestamptz; last_px numeric; last_bid numeric; n int := 0;
  diffs numeric[] := '{}'; prev_mid numeric; v_exit_reason text := null;
  exit_ts timestamptz; exit_px numeric; exit_bid numeric;
begin
  select * into st from saa.shadow_trades where id = p_id and status = 'open';
  if not found then return null; end if;
  select params into v_rules from saa.rules order by version desc limit 1;
  account := coalesce((select value::numeric from saa.settings where key = 'account_size'), 1000);
  bank_frac := coalesce((v_rules->>'partial_bank_frac_of_account')::numeric, 0.075);
  trail_frac := coalesce((v_rules->'trail'->>'frac')::numeric, 0.30);
  tight_frac := coalesce((v_rules->'trail'->>'tight_frac')::numeric, 0.20);
  tight_at := coalesce((v_rules->'trail'->>'tight_at_r')::numeric, 3.0);
  atr_n := coalesce((v_rules->'trail'->>'atr_ticks')::int, 10);
  entry_ask := st.entry_premium;
  spread_abs := greatest(st.entry_mid * st.spread_frac, coalesce((v_rules->'shadow'->>'min_spread_abs')::numeric, 0.02));
  activation_r := (bank_frac * account) / (entry_ask * 100.0);   -- +7.5% of account, in R
  hwm := entry_ask;

  for r in
    select ts, price from saa.price_ticks
    where symbol = st.symbol and ts > st.opened_at and ts <= least(st.window_end, p_now)
    order by ts
  loop
    n := n + 1;
    prem_mid := saa.bs_price(r.price, st.strike, st.sigma_d, saa.t_years(r.ts, st.expiry), st.option_type);
    prem_bid := greatest(prem_mid - spread_abs / 2, 0);
    if prev_mid is not null then
      diffs := diffs || abs(prem_mid - prev_mid);
      if array_length(diffs, 1) > atr_n then diffs := diffs[2:]; end if;
    end if;
    prev_mid := prem_mid;
    gain_r := (prem_bid - entry_ask) / entry_ask;
    mfe := greatest(mfe, gain_r); mae := least(mae, gain_r);
    hwm := greatest(hwm, prem_bid);
    if not trail_on and gain_r >= activation_r then trail_on := true; end if;
    if trail_on then
      atr := coalesce((select avg(x) from unnest(diffs) x), 0);
      stop_px := hwm - greatest((case when gain_r >= tight_at then tight_frac else trail_frac end) * (hwm - entry_ask), atr);
      if prem_bid <= stop_px then
        v_exit_reason := 'trail'; exit_ts := r.ts; exit_px := r.price; exit_bid := prem_bid; exit;
      end if;
    end if;
    last_ts := r.ts; last_px := r.price; last_bid := prem_bid;
  end loop;

  if v_exit_reason is null then
    if p_now >= st.window_end then
      if n = 0 then
        -- no ticks at all: give the feed 30 minutes, then void
        if p_now >= st.window_end + interval '30 minutes' then
          update saa.shadow_trades set status = 'void', exit_reason = 'void_no_data', exit_at = p_now, ticks_used = 0 where id = p_id;
          return 'void_no_data';
        end if;
        return null;
      end if;
      v_exit_reason := case when st.window_end >= st.expiry then 'expiry' else 'time_stop' end;
      exit_ts := last_ts; exit_px := last_px; exit_bid := last_bid;
    else
      return null;   -- still inside the window, no exit yet
    end if;
  end if;

  update saa.shadow_trades set
    status = 'closed', exit_at = exit_ts, exit_underlying = exit_px, exit_premium = round(exit_bid, 4),
    exit_reason = v_exit_reason, r_result = round((exit_bid - entry_ask) / entry_ask, 4),
    mfe_r = round(mfe, 4), mae_r = round(mae, 4), hwm_premium = round(hwm, 4),
    trail_activated = trail_on, ticks_used = n
  where id = p_id;

  -- resolve the checklist + calibration row if this shadow trade belongs to one
  update saa.checklists c set outcome_r = round((exit_bid - entry_ask) / entry_ask, 4), status = 'resolved',
    brier = case when c.probability is null then null else round(power(c.probability - (case when exit_bid > entry_ask then 1 else 0 end), 2), 4) end
  where c.shadow_trade_id = p_id;
  insert into saa.calibration (trade_date, checklist_id, probability, outcome, brier, bucket)
  select c.trade_date, c.id, c.probability, (case when exit_bid > entry_ask then 1 else 0 end), c.brier,
         case when c.probability < 0.2 then '0-20' when c.probability < 0.4 then '20-40' when c.probability < 0.6 then '40-60'
              when c.probability < 0.8 then '60-80' else '80-100' end
  from saa.checklists c where c.shadow_trade_id = p_id and c.probability is not null
  on conflict (checklist_id) do nothing;
  return v_exit_reason;
end $$;

create or replace function saa.score_open_shadow_trades(p_now timestamptz default now())
returns jsonb language plpgsql as $$
declare r record; closed int := 0; voided int := 0; res text; checked int := 0;
begin
  for r in select id from saa.shadow_trades where status = 'open' order by id loop
    checked := checked + 1;
    res := saa.score_shadow_trade(r.id, p_now);
    if res like 'void%' then voided := voided + 1; elsif res is not null then closed := closed + 1; end if;
  end loop;
  return jsonb_build_object('checked', checked, 'closed', closed, 'voided', voided);
end $$;

-- ---------------------------------------------------------------------------
-- In-DB unit tests for the model (run: select saa.test_shadow_model();)
-- ---------------------------------------------------------------------------

create or replace function saa.test_shadow_model() returns jsonb language plpgsql as $$
declare v numeric; v2 numeric; results jsonb := '[]'::jsonb; t0 timestamptz; v_id bigint; res text; st saa.shadow_trades%rowtype;
begin
  -- norm_cdf
  if abs(saa.norm_cdf(0) - 0.5) > 1e-6 then raise exception 'norm_cdf(0) != 0.5'; end if;
  if abs(saa.norm_cdf(1.959964) - 0.975) > 1e-5 then raise exception 'norm_cdf(1.96) != 0.975 (got %)', saa.norm_cdf(1.959964); end if;
  results := results || '"norm_cdf ok"'::jsonb;
  -- ATM call ≈ 0.3989·S·σ·√T for small σ√T (σ_annual = 0.01·√252 = 0.1587; T = 1/252 → σ√T = 0.01)
  v := saa.bs_price(100, 100, 0.01, 1.0/252, 'call');
  if abs(v - 0.3989) > 0.002 then raise exception 'ATM call expected ~0.3989 got %', v; end if;
  -- put-call parity with r = 0: C − P = S − K
  v2 := saa.bs_price(100, 95, 0.02, 3.0/252, 'call') - saa.bs_price(100, 95, 0.02, 3.0/252, 'put');
  if abs(v2 - 5) > 1e-3 then raise exception 'put-call parity failed: %', v2; end if;
  -- expiry → intrinsic
  if saa.bs_price(102, 100, 0.02, 0, 'call') <> 2 then raise exception 'intrinsic at expiry failed'; end if;
  results := results || '"bs_price ok"'::jsonb;
  -- windows
  if saa.window_end_for(saa.et_to_ts(timestamp '2026-09-28 09:45')) <> saa.et_to_ts(timestamp '2026-09-28 10:00') then raise exception 'window 09:45'; end if;
  if saa.window_end_for(saa.et_to_ts(timestamp '2026-09-28 12:15')) is not null then raise exception 'window 12:15 should be null'; end if;
  if saa.window_end_for(saa.et_to_ts(timestamp '2026-09-28 15:45')) is not null then raise exception 'window 15:45 should be null'; end if;
  if saa.next_expiry('SPY', saa.et_to_ts(timestamp '2026-09-28 10:00')) <> saa.et_to_ts(timestamp '2026-09-28 16:00') then raise exception 'index expiry'; end if;
  if saa.next_expiry('ZZTEST', saa.et_to_ts(timestamp '2026-09-28 10:00')) <> saa.et_to_ts(timestamp '2026-10-02 16:00') then raise exception 'single expiry'; end if;
  if abs(saa.t_years(saa.et_to_ts(timestamp '2026-09-28 09:30'), saa.et_to_ts(timestamp '2026-09-28 16:00')) - 1.0/252) > 1e-9 then raise exception 't_years full day'; end if;
  results := results || '"time helpers ok"'::jsonb;

  -- end-to-end: open a synthetic trade on ZZTEST at 09:45, feed a path that runs +3% then fades, expect a trail exit > 0R
  delete from saa.price_ticks where symbol = 'ZZTEST';
  delete from saa.shadow_trades where symbol = 'ZZTEST';
  insert into saa.symbols (symbol, kind, daily_vol, strike_step, spread_frac) values ('ZZTEST', 'single', 0.02, 1.0, 0.05) on conflict (symbol) do nothing;
  t0 := saa.et_to_ts(timestamp '2026-09-28 09:45');
  v_id := saa.open_shadow_trade('ZZTEST', 'orb', 'long', 100.00, t0, 'manual');
  if v_id is null then raise exception 'open_shadow_trade returned null'; end if;
  select * into st from saa.shadow_trades where id = v_id;
  if st.window_end <> saa.et_to_ts(timestamp '2026-09-28 10:00') or st.option_type <> 'call' or st.strike <> 101 then
    raise exception 'unexpected open state: end=% type=% strike=%', st.window_end, st.option_type, st.strike;
  end if;
  insert into saa.price_ticks (symbol, ts, price)
  select 'ZZTEST', t0 + (i || ' minutes')::interval,
         case when i <= 8 then 100 + i * 0.375 else 103 - (i - 8) * 0.5 end
  from generate_series(1, 14) i;
  res := saa.score_shadow_trade(v_id, saa.et_to_ts(timestamp '2026-09-28 10:05'));
  select * into st from saa.shadow_trades where id = v_id;
  if res <> 'trail' or st.r_result is null or st.r_result <= 0 or st.trail_activated is not true then
    raise exception 'expected trail exit with positive R, got reason=% r=% trail=%', res, st.r_result, st.trail_activated;
  end if;
  results := results || jsonb_build_array(jsonb_build_object('e2e', 'ok', 'exit_reason', res, 'r_result', st.r_result, 'mfe_r', st.mfe_r, 'mae_r', st.mae_r, 'entry_premium', st.entry_premium, 'exit_premium', st.exit_premium));
  -- time-stop path: flat tape → small negative R (theta + spread)
  v_id := saa.open_shadow_trade('ZZTEST', 'vwap', 'short', 100.00, saa.et_to_ts(timestamp '2026-09-28 10:05'), 'manual');
  insert into saa.price_ticks (symbol, ts, price)
  select 'ZZTEST', saa.et_to_ts(timestamp '2026-09-28 10:05') + (i || ' minutes')::interval, 100.00 + (i % 3) * 0.05 from generate_series(1, 85) i;
  res := saa.score_shadow_trade(v_id, saa.et_to_ts(timestamp '2026-09-28 11:35'));
  select * into st from saa.shadow_trades where id = v_id;
  if res <> 'time_stop' or st.r_result >= 0 then raise exception 'expected time_stop with negative R, got % %', res, st.r_result; end if;
  results := results || jsonb_build_array(jsonb_build_object('time_stop', 'ok', 'r_result', st.r_result));
  -- cleanup
  delete from saa.shadow_trades where symbol = 'ZZTEST';
  delete from saa.price_ticks where symbol = 'ZZTEST';
  delete from saa.symbols where symbol = 'ZZTEST';
  return results;
end $$;

-- ---------------------------------------------------------------------------
-- Views for the tally / review
-- ---------------------------------------------------------------------------

create or replace view saa.v_shadow_stats as
select lane, source,
       count(*) filter (where status = 'closed') as n,
       round(avg(case when r_result > 0 then 1 else 0 end) filter (where status = 'closed'), 3) as hit_rate,
       round(avg(r_result) filter (where status = 'closed' and r_result > 0), 3) as avg_win_r,
       round(avg(r_result) filter (where status = 'closed' and r_result <= 0), 3) as avg_loss_r,
       round(avg(r_result) filter (where status = 'closed'), 3) as expectancy_r,
       round(sum(r_result) filter (where status = 'closed'), 3) as total_r,
       count(*) filter (where status = 'void') as voided
from saa.shadow_trades group by lane, source;

create or replace view saa.v_brier as
select bucket, count(*) as n, round(avg(probability), 3) as avg_p, round(avg(outcome), 3) as hit_rate, round(avg(brier), 4) as brier
from saa.calibration group by bucket order by bucket;

create or replace view saa.v_daily_records as
select d.trade_date,
       (b.id is not null) as has_brief,
       (select count(*) from saa.checklists c where c.trade_date = d.trade_date) as checklists,
       (select count(*) from saa.shadow_trades s where s.trade_date = d.trade_date) as shadow_trades,
       (select count(*) from saa.shadow_trades s where s.trade_date = d.trade_date and s.status = 'open') as still_open,
       (b.id is not null and (select count(*) from saa.checklists c where c.trade_date = d.trade_date) > 0
          and (select count(*) from saa.shadow_trades s where s.trade_date = d.trade_date and s.status = 'open') = 0) as complete
from saa.calendar_days d left join saa.briefs b on b.trade_date = d.trade_date
order by d.trade_date desc;

-- ---------------------------------------------------------------------------
-- pg_net plumbing: call an edge function from inside Postgres
-- ---------------------------------------------------------------------------

create or replace function saa.call_function(fn text, body jsonb default '{}'::jsonb) returns bigint
language plpgsql security definer set search_path = saa, public, extensions as $$
declare v_base text; v_key text; v_req bigint;
begin
  select s.value into v_base from saa.settings s where s.key = 'functions_base_url';
  select s.value into v_key from saa.settings s where s.key = 'internal_key';
  if v_base is null then raise exception 'saa.settings.functions_base_url not set'; end if;
  select net.http_post(
    url := v_base || '/' || fn,
    body := body,
    headers := jsonb_build_object('Content-Type', 'application/json', 'x-saa-key', v_key),
    timeout_milliseconds := 20000
  ) into v_req;
  return v_req;
end $$;

create or replace function saa.on_outbox_insert() returns trigger
language plpgsql security definer set search_path = saa, public, extensions as $$
begin
  perform saa.call_function('telegram-send', jsonb_build_object('reason', 'outbox_insert', 'id', new.id));
  return new;
end $$;

drop trigger if exists outbox_deliver on saa.outbox;
create trigger outbox_deliver after insert on saa.outbox
  for each row execute function saa.on_outbox_insert();

-- ---------------------------------------------------------------------------
-- RPC surface for the edge functions (public schema, service_role only)
-- ---------------------------------------------------------------------------

create or replace function public.saa_get_setting(p_key text) returns text
language sql security definer set search_path = saa, public as $$ select value from saa.settings where key = p_key $$;

create or replace function public.saa_set_setting(p_key text, p_value text) returns void
language sql security definer set search_path = saa, public as $$
  insert into saa.settings (key, value) values (p_key, p_value)
  on conflict (key) do update set value = excluded.value, updated_at = now() $$;

create or replace function public.saa_log_run(p_job text, p_ok boolean, p_detail jsonb default null) returns void
language sql security definer set search_path = saa, public as $$
  insert into saa.run_log (job, ok, detail) values (p_job, p_ok, p_detail) $$;

create or replace function public.saa_enqueue(p_kind text, p_text text, p_parse_mode text default null) returns bigint
language sql security definer set search_path = saa, public as $$
  insert into saa.outbox (kind, text, parse_mode) values (p_kind, p_text, p_parse_mode) returning id $$;

create or replace function public.saa_outbox_pending(p_limit int default 20)
returns table (id bigint, kind text, body text, parse_mode text, attempts int)
language sql security definer set search_path = saa, public as $$
  select o.id, o.kind, o.text, o.parse_mode, o.attempts from saa.outbox o
  where o.status = 'pending' and o.attempts < 5 order by o.id limit p_limit $$;

create or replace function public.saa_outbox_mark(p_id bigint, p_ok boolean, p_error text default null, p_msg_id bigint default null) returns void
language sql security definer set search_path = saa, public as $$
  update saa.outbox set
    attempts = attempts + 1,
    status = case when p_ok then 'sent' when attempts + 1 >= 5 then 'failed' else 'pending' end,
    sent_at = case when p_ok then now() else sent_at end,
    error = p_error, telegram_message_id = coalesce(p_msg_id, telegram_message_id)
  where id = p_id $$;

create or replace function public.saa_ingest_trigger(p_payload jsonb, p_source_ip text default null) returns jsonb
language plpgsql security definer set search_path = saa, public as $$
declare
  v_sym text; v_lane text; v_dir text; v_price numeric; v_bar timestamptz; v_key text; v_id bigint;
  v_rules jsonb; v_lane_cfg jsonb; v_until time; v_shadow bigint; v_status text := 'received'; v_date date;
  v_now timestamptz := now();
begin
  v_sym := upper(trim(coalesce(p_payload->>'symbol', p_payload->>'ticker', '')));
  v_lane := lower(coalesce(p_payload->>'lane', 'other'));
  v_dir := lower(coalesce(p_payload->>'direction', 'none'));
  if v_dir not in ('long','short') then v_dir := 'none'; end if;
  v_price := nullif(p_payload->>'price', '')::numeric;
  v_bar := case when p_payload ? 'bar_time' and (p_payload->>'bar_time') ~ '^\d+$' then to_timestamp((p_payload->>'bar_time')::bigint / 1000.0)
                when p_payload ? 'bar_time' then nullif(p_payload->>'bar_time', '')::timestamptz else null end;
  if v_sym = '' then return jsonb_build_object('ok', false, 'error', 'missing symbol'); end if;
  v_date := saa.et(v_now)::date;
  v_key := v_sym || '|' || v_lane || '|' || v_dir || '|' || coalesce(to_char(coalesce(v_bar, v_now), 'YYYY-MM-DD HH24:MI'), '');
  insert into saa.triggers (trade_date, symbol, lane, direction, price, bar_time, payload, source_ip, dedupe_key)
  values (v_date, v_sym, v_lane, v_dir, v_price, v_bar, p_payload, p_source_ip, v_key)
  on conflict (dedupe_key) do nothing
  returning id into v_id;
  if v_id is null then return jsonb_build_object('ok', true, 'duplicate', true); end if;

  select params into v_rules from saa.rules order by version desc limit 1;
  v_lane_cfg := v_rules->'fast_lanes'->v_lane;
  if v_lane_cfg is null or coalesce((v_lane_cfg->>'opens_shadow')::boolean, false) is false or v_dir = 'none' or v_price is null then
    v_status := 'flag_only';
  else
    v_until := coalesce((v_lane_cfg->>'entry_until_et')::time, time '15:30');
    if saa.et(v_now)::time > v_until then
      v_status := 'voided';
    else
      v_shadow := saa.open_shadow_trade(v_sym, v_lane, v_dir, v_price, v_now, 'tv', v_id, null);
      v_status := case when v_shadow is null then 'voided' else 'shadow_opened' end;
    end if;
  end if;
  update saa.triggers set status = v_status, shadow_trade_id = v_shadow where id = v_id;
  return jsonb_build_object('ok', true, 'trigger_id', v_id, 'shadow_trade_id', v_shadow, 'status', v_status);
end $$;

create or replace function public.saa_upsert_calendar(p_date date, p_payload jsonb) returns void
language sql security definer set search_path = saa, public as $$
  insert into saa.calendar_days (trade_date, earnings, ipos, econ, vix, index_quotes, errors, fetched_at)
  values (p_date, coalesce(p_payload->'earnings', '[]'::jsonb), coalesce(p_payload->'ipos', '[]'::jsonb),
          coalesce(p_payload->'econ', '[]'::jsonb), coalesce(p_payload->'vix', '{}'::jsonb),
          coalesce(p_payload->'index_quotes', '{}'::jsonb), coalesce(p_payload->'errors', '[]'::jsonb), now())
  on conflict (trade_date) do update set
    earnings = excluded.earnings, ipos = excluded.ipos, econ = case when jsonb_array_length(excluded.econ) > 0 then excluded.econ else saa.calendar_days.econ end,
    vix = excluded.vix, index_quotes = excluded.index_quotes, errors = excluded.errors, fetched_at = now(), updated_at = now() $$;

create or replace function public.saa_snapshot(p_kind text, p_payload jsonb) returns void
language sql security definer set search_path = saa, public as $$
  insert into saa.market_snapshots (kind, payload) values (p_kind, p_payload) $$;

create or replace function public.saa_active_symbols() returns text[]
language sql security definer set search_path = saa, public as $$
  select array_agg(distinct s order by s) from (
    select unnest(string_to_array(coalesce((select value from saa.settings where key = 'index_symbols'), 'SPY,QQQ,IWM'), ',')) as s
    union select symbol from saa.shadow_trades where status = 'open'
    union select symbol from saa.triggers where trade_date = saa.et(now())::date
    union select jsonb_array_elements_text(watch) from saa.calendar_days where trade_date = saa.et(now())::date
    union select symbol from saa.checklists where trade_date = saa.et(now())::date and decision <> 'stand_down'
  ) x where s is not null and s <> '' $$;

create or replace function public.saa_insert_ticks(p_rows jsonb) returns int
language plpgsql security definer set search_path = saa, public as $$
declare n int;
begin
  insert into saa.price_ticks (symbol, ts, price, day_open, day_high, day_low, prev_close)
  select upper(r->>'symbol'), (r->>'ts')::timestamptz, (r->>'price')::numeric,
         nullif(r->>'day_open','')::numeric, nullif(r->>'day_high','')::numeric, nullif(r->>'day_low','')::numeric, nullif(r->>'prev_close','')::numeric
  from jsonb_array_elements(p_rows) r
  where (r->>'price') is not null
  on conflict (symbol, ts) do nothing;
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.saa_score_shadow_trades() returns jsonb
language sql security definer set search_path = saa, public as $$ select saa.score_open_shadow_trades(now()) $$;

create or replace function public.saa_watch_add(p_symbols text[]) returns void
language sql security definer set search_path = saa, public as $$
  insert into saa.calendar_days (trade_date, watch) values (saa.et(now())::date, to_jsonb(p_symbols))
  on conflict (trade_date) do update set watch = (
    select to_jsonb(array_agg(distinct x)) from (
      select jsonb_array_elements_text(saa.calendar_days.watch) x union select unnest(p_symbols)) u), updated_at = now() $$;

-- Lock the RPC surface down to the service role.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname like 'saa\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    execute format('grant execute on function %s to service_role', f.sig);
  end loop;
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'saa' loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
  end loop;
end $$;
