-- 0004_function_search_path.sql — pin search_path on every saa.* function (security linter 0011).
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'saa' loop
    execute format('alter function %s set search_path = saa, public, extensions', f.sig);
  end loop;
end $$;
