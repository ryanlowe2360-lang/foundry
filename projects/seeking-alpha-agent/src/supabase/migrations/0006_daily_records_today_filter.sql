-- 0006_daily_records_today_filter.sql — applied 2026-09-27.
-- saa.calendar_days now holds future rows (the next trading day pre-fetched by market-data, and the
-- hand-maintained FOMC/CPI/NFP dates loaded by saa_econ_upsert). The M1→M2 record streak is counted
-- "consecutive complete days from the most recent" over `select * from saa.v_daily_records limit N`,
-- so future dates must not appear in the view or they read as incomplete days.
create or replace view saa.v_daily_records as
select d.trade_date,
       (b.id is not null) as has_brief,
       (select count(*) from saa.checklists c where c.trade_date = d.trade_date) as checklists,
       (select count(*) from saa.shadow_trades s where s.trade_date = d.trade_date) as shadow_trades,
       (select count(*) from saa.shadow_trades s where s.trade_date = d.trade_date and s.status = 'open') as still_open,
       (b.id is not null and (select count(*) from saa.checklists c where c.trade_date = d.trade_date) > 0
          and (select count(*) from saa.shadow_trades s where s.trade_date = d.trade_date and s.status = 'open') = 0) as complete
from saa.calendar_days d left join saa.briefs b on b.trade_date = d.trade_date
where d.trade_date <= saa.et(now())::date
  and extract(isodow from d.trade_date) <= 5
order by d.trade_date desc;
