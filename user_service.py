"""
User management + free-generation-limit tracking, backed by Supabase.

Design notes
------------
* `users` holds the profile (language, allergies, appliances, chef persona,
  premium/trial state, referral link).
* `daily_usage` holds one row per (user_id, usage_date). Because the row is
  keyed by date, the "reset at 00:00" requirement falls out naturally: once
  the calendar day changes there simply is no row yet for today, so the
  counter starts back at zero without any cron job or scheduled task.
* Every Supabase SDK call is synchronous internally, so it is offloaded to a
  worker thread via `anyio.to_thread.run_sync` to keep route handlers
  non-blocking.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import anyio
from postgrest.exceptions import APIError

from config import get_settings
from database import get_supabase_client
from models import (
    ChefPersona,
    Language,
    UserProfile,
    UserRegisterRequest,
    UserSettingsUpdateRequest,
    UsageStatus,
)

settings = get_settings()


class LimitExceededError(Exception):
    """Raised when a user has exhausted today's free generations and has no premium/bonus left."""


class UserNotFoundError(Exception):
    pass


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _next_midnight_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


async def _run(fn, *args, **kwargs):
    """Run a blocking supabase-py call in a worker thread."""
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))


def _row_to_profile(row: dict[str, Any]) -> UserProfile:
    return UserProfile(
        id=row["id"],
        external_id=row["external_id"],
        language=Language(row.get("language", "en")),
        allergies=row.get("allergies") or [],
        appliances=row.get("appliances") or [],
        chef_persona=ChefPersona(row.get("chef_persona", "classic")),
        is_premium=bool(row.get("is_premium", False)),
        premium_expires_at=row.get("premium_expires_at"),
        trial_used=bool(row.get("trial_used", False)),
        referred_by=row.get("referred_by"),
        created_at=row.get("created_at"),
    )


async def get_or_create_user(payload: UserRegisterRequest) -> UserProfile:
    client = get_supabase_client()

    def _fetch():
        return client.table("users").select("*").eq("external_id", payload.external_id).limit(1).execute()

    result = await _run(_fetch)
    if result.data:
        return _row_to_profile(result.data[0])

    new_row = {
        "external_id": payload.external_id,
        "source": payload.source,
        "language": payload.language.value,
        "allergies": [],
        "appliances": [],
        "chef_persona": ChefPersona.classic.value,
        "is_premium": False,
        "trial_used": False,
        "referred_by": payload.referral_code,
    }

    def _insert():
        return client.table("users").insert(new_row).execute()

    inserted = await _run(_insert)
    created = _row_to_profile(inserted.data[0])

    # Referral bonus: grant the referrer extra requests for today, same as
    # the original workflow's "Начислить бонус" / "Уведомить пригласившего" step.
    if payload.referral_code:
        await _grant_bonus_to_referrer(payload.referral_code)

    return created


async def _grant_bonus_to_referrer(referrer_external_id: str) -> None:
    client = get_supabase_client()

    def _fetch_referrer():
        return client.table("users").select("id").eq("external_id", referrer_external_id).limit(1).execute()

    result = await _run(_fetch_referrer)
    if not result.data:
        return  # referral code did not match any known user — ignore silently

    referrer_id = result.data[0]["id"]
    await _adjust_bonus_requests(referrer_id, delta=settings.REFERRAL_BONUS_REQUESTS)


async def get_user_by_id(user_id: str) -> UserProfile:
    client = get_supabase_client()

    def _fetch():
        return client.table("users").select("*").eq("id", user_id).limit(1).execute()

    result = await _run(_fetch)
    if not result.data:
        raise UserNotFoundError(f"No user with id={user_id}")
    return _row_to_profile(result.data[0])


async def update_settings(user_id: str, update: UserSettingsUpdateRequest) -> UserProfile:
    client = get_supabase_client()
    patch: dict[str, Any] = {}
    if update.language is not None:
        patch["language"] = update.language.value
    if update.allergies is not None:
        patch["allergies"] = update.allergies
    if update.appliances is not None:
        patch["appliances"] = [a.value for a in update.appliances]
    if update.chef_persona is not None:
        patch["chef_persona"] = update.chef_persona.value

    if not patch:
        return await get_user_by_id(user_id)

    def _update():
        return client.table("users").update(patch).eq("id", user_id).execute()

    result = await _run(_update)
    if not result.data:
        raise UserNotFoundError(f"No user with id={user_id}")
    return _row_to_profile(result.data[0])


async def activate_trial(user_id: str) -> UserProfile:
    """Mirrors 'Триал доступен? / Триал уже использован? / Активировать триал'."""
    profile = await get_user_by_id(user_id)
    if profile.trial_used:
        raise LimitExceededError("Trial has already been used for this account.")

    client = get_supabase_client()

    def _update():
        return client.table("users").update({"trial_used": True}).eq("id", user_id).execute()

    await _run(_update)
    await _adjust_bonus_requests(user_id, delta=settings.TRIAL_BONUS_REQUESTS)
    return await get_user_by_id(user_id)


async def _get_or_create_today_usage_row(user_id: str) -> dict[str, Any]:
    client = get_supabase_client()
    today = _today_str()

    def _fetch():
        return (
            client.table("daily_usage")
            .select("*")
            .eq("user_id", user_id)
            .eq("usage_date", today)
            .limit(1)
            .execute()
        )

    result = await _run(_fetch)
    if result.data:
        return result.data[0]

    new_row = {"user_id": user_id, "usage_date": today, "used_count": 0, "bonus_requests": 0}

    def _insert():
        return client.table("daily_usage").insert(new_row).execute()

    inserted = await _run(_insert)
    return inserted.data[0]


async def _adjust_bonus_requests(user_id: str, delta: int) -> None:
    """Adds bonus (referral/trial) requests onto *today's* usage row."""
    row = await _get_or_create_today_usage_row(user_id)
    client = get_supabase_client()
    new_bonus = max(0, row.get("bonus_requests", 0) + delta)

    def _update():
        return (
            client.table("daily_usage")
            .update({"bonus_requests": new_bonus})
            .eq("user_id", user_id)
            .eq("usage_date", row["usage_date"])
            .execute()
        )

    await _run(_update)


async def get_usage_status(user_id: str) -> UsageStatus:
    profile = await get_user_by_id(user_id)
    row = await _get_or_create_today_usage_row(user_id)
    used = row.get("used_count", 0)
    bonus = row.get("bonus_requests", 0)

    if profile.is_premium:
        remaining = 10**6  # effectively unlimited while premium is active
    else:
        remaining = max(0, settings.DAILY_FREE_LIMIT + bonus - used)

    return UsageStatus(
        date=row["usage_date"],
        used_today=used,
        daily_limit=settings.DAILY_FREE_LIMIT,
        bonus_requests=bonus,
        remaining=remaining,
        is_premium=profile.is_premium,
        resets_at=_next_midnight_utc_iso(),
    )


async def consume_generation_slot(user_id: str) -> UsageStatus:
    """
    Atomically checks the daily limit and increments the counter.
    Raises LimitExceededError if the user has none left (mirrors the
    original 'Лимит не превышен' -> 'Лимит исчерпан' branch).
    """
    profile = await get_user_by_id(user_id)
    row = await _get_or_create_today_usage_row(user_id)
    used = row.get("used_count", 0)
    bonus = row.get("bonus_requests", 0)

    if not profile.is_premium and used >= settings.DAILY_FREE_LIMIT + bonus:
        raise LimitExceededError(
            f"Daily free limit reached ({settings.DAILY_FREE_LIMIT} + {bonus} bonus). "
            f"Resets at {_next_midnight_utc_iso()}."
        )

    client = get_supabase_client()

    def _increment():
        return (
            client.table("daily_usage")
            .update({"used_count": used + 1})
            .eq("user_id", user_id)
            .eq("usage_date", row["usage_date"])
            .execute()
        )

    try:
        await _run(_increment)
    except APIError as exc:  # pragma: no cover - defensive, surfaces a clean 502 upstream
        raise RuntimeError(f"Supabase update failed: {exc}") from exc

    return await get_usage_status(user_id)
