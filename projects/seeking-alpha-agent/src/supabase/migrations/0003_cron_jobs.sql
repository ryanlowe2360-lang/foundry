-- 0003_cron_jobs.sql — pg_cron schedule (applied 2026-09-26). Schedules are UTC; every edge
-- function self-gates on America/New_York time, so both EDT and EST slots exist where needed.
-- Re-runnable: unschedule first.
do $$ declare j record; begin
  for j in select jobname from cron.job where jobname like 'saa\_%' loop perform cron.unschedule(j.jobname); end loop;
end $$;
select cron.schedule('saa_calendar_edt',   '15 11 * * 1-5',    $$select saa.call_function('market-data', '{"mode":"calendar"}'::jsonb)$$);
select cron.schedule('saa_calendar_est',   '15 12 * * 1-5',    $$select saa.call_function('market-data', '{"mode":"calendar"}'::jsonb)$$);
select cron.schedule('saa_open_snapshot',  '28 13,14 * * 1-5', $$select saa.call_function('market-data', '{"mode":"open_snapshot"}'::jsonb)$$);
select cron.schedule('saa_quotes',         '* 13-21 * * 1-5',  $$select saa.call_function('market-data', '{"mode":"quotes"}'::jsonb)$$);
select cron.schedule('saa_scorer',         '*/5 13-21 * * 1-5', $$select saa.call_function('shadow-scorer', '{}'::jsonb)$$);
select cron.schedule('saa_telegram_poll',  '*/2 * * * *',      $$select saa.call_function('telegram-send', '{"reason":"poll"}'::jsonb)$$);
select cron.schedule('saa_housekeeping',   '0 6 * * *',        $$delete from saa.price_ticks where ts < now() - interval '90 days'; delete from saa.run_log where ts < now() - interval '60 days'; delete from saa.market_snapshots where ts < now() - interval '180 days'$$);
