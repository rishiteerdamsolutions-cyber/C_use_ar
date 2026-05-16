-- Add subscription / access columns (safe if already present).
-- Run in Supabase SQL Editor if you created the DB before these columns existed.

alter table public.cusear_users
    add column if not exists active boolean not null default true;

alter table public.cusear_users
    add column if not exists razorpay_subscription_id text unique;

create index if not exists idx_cusear_users_razorpay_sub
    on public.cusear_users (razorpay_subscription_id);
