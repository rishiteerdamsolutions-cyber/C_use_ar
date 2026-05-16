-- cusear™ Web UI + Local Agent — PostgreSQL schema (Supabase)
-- Run in Supabase SQL Editor after project creation.
-- Service role bypasses RLS; browser clients use anon key + RLS policies.

-- Extensions
create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Users (product row; link to auth.users when using Supabase Auth)
-- ---------------------------------------------------------------------------
create table if not exists public.cusear_users (
    id              uuid primary key default gen_random_uuid(),
    auth_user_id    uuid unique references auth.users (id) on delete set null,
    email           text unique,
    phone           text,
    agent_token     text unique not null,
    plan            text,
    active          boolean not null default true,
    razorpay_subscription_id text unique,
    subscribed_at   timestamptz,
    expires_at      timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists idx_cusear_users_agent_token on public.cusear_users (agent_token);
create index if not exists idx_cusear_users_razorpay_sub on public.cusear_users (razorpay_subscription_id);

-- ---------------------------------------------------------------------------
-- Workflows (JSON synced to local agent)
-- ---------------------------------------------------------------------------
create table if not exists public.cusear_workflows (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.cusear_users (id) on delete cascade,
    name            text not null,
    platform        text,
    workflow_json   jsonb not null default '{}'::jsonb,
    enriched_at     timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (user_id, name)
);

create index if not exists idx_cusear_workflows_user on public.cusear_workflows (user_id);

-- ---------------------------------------------------------------------------
-- Schedules
-- ---------------------------------------------------------------------------
create table if not exists public.cusear_schedules (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.cusear_users (id) on delete cascade,
    workflow_id     uuid references public.cusear_workflows (id) on delete set null,
    run_time        time,
    days            text[] default array[]::text[],
    active          boolean not null default true,
    content_map     jsonb default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists idx_cusear_schedules_user on public.cusear_schedules (user_id);

-- ---------------------------------------------------------------------------
-- Run history
-- ---------------------------------------------------------------------------
create table if not exists public.cusear_runs (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.cusear_users (id) on delete cascade,
    workflow_id     uuid references public.cusear_workflows (id) on delete set null,
    workflow_name   text,
    status          text not null default 'queued',
    report_secret   text,
    error           text,
    screenshot_path text,
    meta            jsonb default '{}'::jsonb,
    started_at      timestamptz,
    completed_at    timestamptz,
    created_at      timestamptz not null default now()
);

create index if not exists idx_cusear_runs_user on public.cusear_runs (user_id);
create index if not exists idx_cusear_runs_status on public.cusear_runs (status);

-- ---------------------------------------------------------------------------
-- Agent connection / health (dashboard live dot)
-- ---------------------------------------------------------------------------
create table if not exists public.cusear_agent_status (
    user_id         uuid primary key references public.cusear_users (id) on delete cascade,
    connected       boolean not null default false,
    os              text,
    agent_version   text,
    last_seen       timestamptz,
    chrome_ok       boolean,
    disk_free_mb    integer,
    workflows       text[] default array[]::text[],
    updated_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- RLS (optional; dashboard using service role on server can skip client RLS)
-- Enable when the browser uses Supabase anon key directly.
-- ---------------------------------------------------------------------------
alter table public.cusear_users enable row level security;
alter table public.cusear_workflows enable row level security;
alter table public.cusear_schedules enable row level security;
alter table public.cusear_runs enable row level security;
alter table public.cusear_agent_status enable row level security;

-- Policies: owner = auth.uid() matches auth_user_id on cusear_users
create policy "users_own_row"
    on public.cusear_users for select
    using (auth.uid() = auth_user_id);

create policy "users_update_own"
    on public.cusear_users for update
    using (auth.uid() = auth_user_id);

create policy "workflows_by_owner"
    on public.cusear_workflows for all
    using (
        exists (
            select 1 from public.cusear_users u
            where u.id = cusear_workflows.user_id and u.auth_user_id = auth.uid()
        )
    );

create policy "schedules_by_owner"
    on public.cusear_schedules for all
    using (
        exists (
            select 1 from public.cusear_users u
            where u.id = cusear_schedules.user_id and u.auth_user_id = auth.uid()
        )
    );

create policy "runs_by_owner"
    on public.cusear_runs for all
    using (
        exists (
            select 1 from public.cusear_users u
            where u.id = cusear_runs.user_id and u.auth_user_id = auth.uid()
        )
    );

create policy "agent_status_by_owner"
    on public.cusear_agent_status for all
    using (
        exists (
            select 1 from public.cusear_users u
            where u.id = cusear_agent_status.user_id and u.auth_user_id = auth.uid()
        )
    );

-- Optional: add BEFORE UPDATE triggers for updated_at in the Supabase UI if desired.
