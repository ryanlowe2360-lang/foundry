-- 0002_checklist_shadows.sql — open the pre-market brief's shadow trades at window start,
-- resolve two-sided (strangle) checklists, and a /status text for Telegram.

-- The 7:40 brief writes checklists with decision = 'shadow' and a window_start/window_end.
-- The entry price is unknown pre-market, so the shadow trade opens on the first tick at or
-- after window_start (the scorer calls this every few minutes during the session).
create or replace function saa.open_checklist_shadows(p_now timestamptz default now())
returns int language plpgsql as $$
declare c record; t record; v_id bigint; v_id2 bigint; opened int := 0;
begin
  for c in
    select * from saa.checklists
    where decision = 'shadow' and shadow_trade_id is null and status = 'open'
      and direction in ('long','short','two_sided')
      and window_start is not null and window_end is not null
      and window_start <= p_now and window_end > p_now
    order by id
  loop
    select ts, price into t from saa.price_ticks
    where symbol = c.symbol and ts >= c.window_start and ts <= p_now order by ts limit 1;
    if not found then continue; end if;
    if c.direction = 'two_sided' then
      v_id  := saa.open_shadow_trade(c.symbol, 'brief', 'long',  t.price, t.ts, 'brief', null, c.id, c.window_end);
      v_id2 := saa.open_shadow_trade(c.symbol, 'brief', 'short', t.price, t.ts, 'brief', null, c.id, c.window_end);
      if v_id is null then v_id := v_id2; end if;
    else
      v_id := saa.open_shadow_trade(c.symbol, 'brief', c.direction, t.price, t.ts, 'brief', null, c.id, c.window_end);
    end if;
    if v_id is null then
      update saa.checklists set status = 'void', decision = 'stand_down' where id = c.id;
    else
      update saa.checklists set shadow_trade_id = v_id where id = c.id;
      opened := opened + 1;
    end if;
  end loop;
  -- checklists whose window passed with no tick at all → void (feed gap), never silently open
  update saa.checklists set status = 'void'
  where decision = 'shadow' and shadow_trade_id is null and status = 'open'
    and window_end is not null and window_end + interval '30 minutes' < p_now;
  return opened;
end $$;

-- Two-sided checklists: outcome is the average R of the two legs once both are closed
-- (equal premium per leg → combined R relative to total premium).
create or replace function saa.resolve_two_sided_checklists() returns int language plpgsql as $$
declare c record; v_r numeric; v_n int; v_open int; done int := 0;
begin
  for c in select * from saa.checklists where direction = 'two_sided' and status = 'open' and shadow_trade_id is not null loop
    select count(*) filter (where status = 'open'), count(*) filter (where status = 'closed'), avg(r_result) filter (where status = 'closed')
      into v_open, v_n, v_r from saa.shadow_trades where checklist_id = c.id;
    if v_open = 0 and v_n > 0 then
      update saa.checklists set outcome_r = round(v_r, 4), status = 'resolved',
        brier = case when probability is null then null else round(power(probability - (case when v_r > 0 then 1 else 0 end), 2), 4) end
      where id = c.id;
      insert into saa.calibration (trade_date, checklist_id, probability, outcome, brier, bucket)
      select trade_date, id, probability, (case when v_r > 0 then 1 else 0 end), brier,
             case when probability < 0.2 then '0-20' when probability < 0.4 then '20-40' when probability < 0.6 then '40-60'
                  when probability < 0.8 then '60-80' else '80-100' end
      from saa.checklists where id = c.id and probability is not null
      on conflict (checklist_id) do nothing;
      done := done + 1;
    end if;
  end loop;
  return done;
end $$;

-- The single-leg path in score_shadow_trade resolves its checklist by shadow_trade_id; a two-sided
-- checklist links to the call leg, so guard that path against double-resolution: only single-leg.
create or replace function public.saa_score_shadow_trades() returns jsonb
language plpgsql security definer set search_path = saa, public as $$
declare v_opened int; v_scored jsonb; v_two int;
begin
  v_opened := saa.open_checklist_shadows(now());
  v_scored := saa.score_open_shadow_trades(now());
  v_two := saa.resolve_two_sided_checklists();
  return v_scored || jsonb_build_object('checklist_shadows_opened', v_opened, 'two_sided_resolved', v_two);
end $$;

-- Short status text for Telegram (/status) and the tally.
create or replace function public.saa_status_text() returns text
language plpgsql security definer set search_path = saa, public as $$
declare d date := saa.et(now())::date; v_brief boolean; v_open int; v_closed int; v_r numeric; v_n int; v_hit numeric; v_exp numeric; v_lines text;
begin
  select exists(select 1 from saa.briefs where trade_date = d) into v_brief;
  select count(*) filter (where status = 'open'), count(*) filter (where status = 'closed'), coalesce(sum(r_result) filter (where status = 'closed'), 0)
    into v_open, v_closed, v_r from saa.shadow_trades where trade_date = d;
  select count(*), round(avg(case when r_result > 0 then 1 else 0 end), 3), round(avg(r_result), 3)
    into v_n, v_hit, v_exp from saa.shadow_trades where status = 'closed';
  v_lines := format('SAA status %s ET%sBrief today: %s%sShadow trades today: %s open, %s closed, %s R%sLedger to date: n=%s, hit rate %s, expectancy %s R/trade',
    to_char(saa.et(now()), 'Dy HH24:MI'), E'\n', case when v_brief then 'yes' else 'no' end, E'\n', v_open, v_closed, round(v_r, 2), E'\n',
    v_n, coalesce(v_hit::text, '—'), coalesce(v_exp::text, '—'));
  return v_lines;
end $$;

do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where (n.nspname = 'public' and p.proname like 'saa\_%') or n.nspname = 'saa' loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    if f.sig::text like 'public.%' then execute format('grant execute on function %s to service_role', f.sig); end if;
  end loop;
end $$;
