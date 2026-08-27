-- AI Chef — Supabase (PostgreSQL) schema
-- Run this in the Supabase SQL editor (or via `supabase db push`) before
-- starting the backend.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- users: one row per app/Telegram user
-- ---------------------------------------------------------------------------
create table if not exists public.users (
    id                uuid primary key default gen_random_uuid(),
    external_id       text not null unique,           -- telegram_id or mobile device/auth id
    source            text not null default 'app',    -- 'app' | 'telegram'
    language          text not null default 'en' check (language in ('ru', 'ua', 'en')),
    allergies         text[] not null default '{}',
    appliances        text[] not null default '{}',
    chef_persona      text not null default 'classic'
                      check (chef_persona in ('classic', 'cute', 'michelin', 'rude', 'barinov')),
    is_premium        boolean not null default false,
    premium_expires_at timestamptz,
    trial_used        boolean not null default false,
    referred_by       text,                            -- external_id of the referrer, if any
    created_at        timestamptz not null default now()
);

create index if not exists idx_users_external_id on public.users (external_id);

-- ---------------------------------------------------------------------------
-- daily_usage: one row per (user, calendar day). Absence of today's row is
-- what makes the free-limit counter "reset at 00:00" with zero extra logic.
-- ---------------------------------------------------------------------------
create table if not exists public.daily_usage (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.users (id) on delete cascade,
    usage_date      date not null default (now() at time zone 'utc')::date,
    used_count      integer not null default 0,
    bonus_requests  integer not null default 0,        -- from referrals / trial activation
    unique (user_id, usage_date)
);

create index if not exists idx_daily_usage_user_date on public.daily_usage (user_id, usage_date);

-- ---------------------------------------------------------------------------
-- recipe_history (optional but recommended): keeps a record of what was
-- generated, useful for "regenerate last dish photo" and analytics.
-- ---------------------------------------------------------------------------
create table if not exists public.recipe_history (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.users (id) on delete cascade,
    dish_name       text not null,
    recipe_json     jsonb not null,
    image_url       text,
    created_at      timestamptz not null default now()
);

create index if not exists idx_recipe_history_user on public.recipe_history (user_id, created_at desc);

-- Row Level Security is left disabled here because all access goes through
-- the backend using the Supabase SECRET (service_role) key, which bypasses
-- RLS by design. If you later expose Supabase directly to client apps with
-- the publishable key, enable RLS and add policies scoped to auth.uid().
