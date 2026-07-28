-- Phase 2: datapoint storage, scoring, catalog. Run in the Supabase SQL editor
-- after 001_quota.sql. All tables are RLS-locked to the service key.

-- Tracked keywords (the seed list, mirrored from config so joins are possible)
create table if not exists keywords (
    keyword    text primary key,        -- slug: 'mushroom-lamp'
    display    text not null,           -- 'mushroom lamp'
    category   text,
    active     boolean not null default true,
    added_at   timestamptz not null default now()
);

-- The normalized time series every collector writes to.
-- region '' means global (empty string, not NULL, so the uniqueness
-- constraint works with ON CONFLICT upserts).
create table if not exists trend_datapoints (
    id           bigint generated always as identity primary key,
    keyword      text not null,
    source       text not null,
    metric       text not null,
    value        double precision not null,
    region       text not null default '',
    observed_at  timestamptz not null,
    collected_at timestamptz not null,
    meta         jsonb not null default '{}'::jsonb,
    raw_ref      text not null default '',
    unique (keyword, source, metric, region, observed_at)
);
create index if not exists idx_datapoints_series
    on trend_datapoints (keyword, metric, observed_at);
create index if not exists idx_datapoints_region
    on trend_datapoints (metric, region) where region <> '';

-- ISO region reference (joined by the dashboard for display names)
create table if not exists regions (
    code text primary key,              -- 'US', 'US-CA', 'GB'
    name text not null
);

-- Scoring runs: one row per keyword per run, full breakdown in components
create table if not exists scores (
    id           bigint generated always as identity primary key,
    keyword      text not null,
    computed_at  timestamptz not null default now(),
    as_of        timestamptz not null,     -- data cutoff the score was computed from
    composite    double precision not null,
    components   jsonb not null,           -- momentum/seasonality/regional/saturation detail
    confidence   text not null default 'normal',  -- normal | low (sparse data)
    backtest     boolean not null default false
);
create index if not exists idx_scores_latest on scores (keyword, as_of desc);

-- Phase 3 writes these; created now so the schema is complete
create table if not exists recommendations (
    id          bigint generated always as identity primary key,
    keyword     text not null,
    created_at  timestamptz not null default now(),
    score       double precision,
    analysis    jsonb not null,             -- structured Claude output
    status      text not null default 'new' -- new | launched | skipped | winner | loser
        check (status in ('new','launched','skipped','winner','loser')),
    status_note text
);

-- Products the user already sells (early-warning tracking in Phase 4)
create table if not exists my_catalog (
    id         bigint generated always as identity primary key,
    keyword    text not null,
    product    text not null,
    added_at   timestamptz not null default now(),
    active     boolean not null default true,
    notes      text
);

-- Data-retention commitment (attested in the AliExpress compliance form):
-- AliExpress-sourced rows are kept ~12 months, everything is deletable on
-- request. Run monthly (Supabase cron or manually).
create or replace function purge_expired_datapoints(p_retention_days integer default 365)
returns integer
language sql
as $$
    with deleted as (
        delete from trend_datapoints
        where source = 'aliexpress'
          and collected_at < now() - make_interval(days => p_retention_days)
        returning 1
    )
    select count(*)::integer from deleted;
$$;

alter table keywords         enable row level security;
alter table trend_datapoints enable row level security;
alter table regions          enable row level security;
alter table scores           enable row level security;
alter table recommendations  enable row level security;
alter table my_catalog       enable row level security;
