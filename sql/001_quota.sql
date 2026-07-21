-- Phase 1: API quota tracking (run this in the Supabase SQL editor).
-- Tracks paid/capped API usage per calendar month so scheduled runs
-- hard-stop before burning a free tier. Phase 2 adds the datapoint tables.

create table if not exists api_quota (
    source text not null,           -- e.g. 'serpapi'
    month  text not null,           -- 'YYYY-MM' (UTC)
    used   integer not null default 0,
    updated_at timestamptz not null default now(),
    primary key (source, month)
);

-- Atomic increment used by the collector (avoids read-modify-write races
-- if two runs ever overlap).
create or replace function increment_quota(p_source text, p_month text, p_n integer)
returns integer
language sql
as $$
    insert into api_quota (source, month, used)
    values (p_source, p_month, p_n)
    on conflict (source, month)
    do update set used = api_quota.used + p_n, updated_at = now()
    returning used;
$$;
