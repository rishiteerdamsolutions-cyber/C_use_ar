"""
API Key Manager — generate, hash, validate, store.
cusear™ Platform · API Layer

Key format:  ak_live_<32 random chars>   (production)
             ak_test_<32 random chars>   (sandbox)

Only the SHA-256 hash is stored in MongoDB — never the raw key.
The raw key is shown to the user exactly once at creation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV = os.environ.get("API_ENV", "live")   # "live" or "test"


# ─── Hashing ──────────────────────────────────────────────────────────────────
def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of a raw API key — this is what we store."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ─── Generation ───────────────────────────────────────────────────────────────
def generate_api_key() -> tuple[str, str]:
    """
    Generate a new API key pair.

    Returns:
        (raw_key, hashed_key)
        raw_key    — shown to user once:  ak_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
        hashed_key — stored in MongoDB
    """
    prefix  = f"ak_{ENV}_"
    token   = secrets.token_urlsafe(24)   # 32-char URL-safe random string
    raw_key = f"{prefix}{token}"
    return raw_key, _hash_key(raw_key)


# ─── CRUD ─────────────────────────────────────────────────────────────────────
def create_key(
    owner_name:  str,
    owner_email: str,
    credits:     int,
    pack:        str,
    razorpay_payment_id: Optional[str] = None,
    **kwargs: Any,
) -> tuple[str, str]:
    """
    Create and persist a new API key in MongoDB.

    Args:
        owner_name:           Display name of the key owner.
        owner_email:          Email address.
        credits:              Starting credit balance.
        pack:                 Credit pack name (starter / professional / agency).
        razorpay_payment_id:  Payment ID if key was paid for.

    Returns:
        (raw_key, key_id)
        raw_key — full key string to show the user (ONCE only)
        key_id  — MongoDB _id as string
    """
    from agency_api.database import get_collection, Collections

    raw_key, hashed = generate_api_key()

    doc: dict[str, Any] = {
        "key_hash":             hashed,
        "owner_name":           owner_name,
        "owner_email":          owner_email,
        "credits_total":        credits,
        "credits_used":         0,
        "status":               "active",
        "pack":                 pack,
        "razorpay_payment_id":  razorpay_payment_id,
        "created_at":           datetime.now(timezone.utc),
        "last_used_at":         None,
        "calls_today":          0,
        "calls_this_month":     0,
        "day_reset":            datetime.now(timezone.utc).date().isoformat(),
        "month_reset":          datetime.now(timezone.utc).strftime("%Y-%m"),
        "ai_runs_this_month":     0,
        "ai_runs_month_reset":    datetime.now(timezone.utc).strftime("%Y-%m"),
    }
    for k, v in (kwargs or {}).items():
        if k and not str(k).startswith("_"):
            doc[k] = v

    col    = get_collection(Collections.API_KEYS)
    result = col.insert_one(doc)
    key_id = str(result.inserted_id)

    logger.info(
        "API key created  key_id=%s  owner=%s  credits=%d  pack=%s",
        key_id, owner_email, credits, pack,
    )
    return raw_key, key_id


def validate_key(raw_key: str) -> Optional[dict[str, Any]]:
    """
    Validate an API key and return its document if active.

    Args:
        raw_key: Full key string from the request header.

    Returns:
        Key document dict if valid and active, None otherwise.
    """
    from agency_api.database import get_collection, Collections

    if not raw_key or not raw_key.startswith("ak_"):
        return None

    hashed = _hash_key(raw_key)
    col    = get_collection(Collections.API_KEYS)
    doc    = col.find_one({"key_hash": hashed, "status": "active"})

    if doc:
        logger.debug("Key validated — key_id=%s owner=%s", doc["_id"], doc["owner_email"])
    else:
        logger.warning("Invalid or inactive API key presented")

    return doc


def get_key_by_id(key_id: str) -> Optional[dict[str, Any]]:
    """Fetch a key document by its MongoDB _id string."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    col = get_collection(Collections.API_KEYS)
    return col.find_one({"_id": ObjectId(key_id)})


def suspend_key(key_id: str) -> bool:
    """Suspend an API key (blocks all future requests)."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    col    = get_collection(Collections.API_KEYS)
    result = col.update_one(
        {"_id": ObjectId(key_id)},
        {"$set": {"status": "suspended"}},
    )
    logger.info("Key suspended: %s", key_id)
    return result.modified_count > 0


def top_up_credits(key_id: str, credits: int, razorpay_payment_id: str) -> int:
    """
    Add credits to an existing key after successful Razorpay payment.

    Returns:
        New total credits balance.
    """
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    col = get_collection(Collections.API_KEYS)
    col.update_one(
        {"_id": ObjectId(key_id)},
        {
            "$inc": {"credits_total": credits},
            "$set": {"status": "active"},    # reactivate if suspended
        },
    )
    doc = col.find_one({"_id": ObjectId(key_id)})
    new_total = doc["credits_total"] - doc["credits_used"]

    logger.info(
        "Credits topped up  key_id=%s  added=%d  razorpay=%s  new_balance=%d",
        key_id, credits, razorpay_payment_id, new_total,
    )
    return new_total


def deduct_credits(key_id: str, amount: int) -> tuple[bool, int]:
    """
    Deduct credits from a key atomically.

    Args:
        key_id: MongoDB _id string.
        amount: Credits to deduct.

    Returns:
        (success, remaining_credits)
        success=False if balance would go negative.
    """
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    col = get_collection(Collections.API_KEYS)
    doc = col.find_one({"_id": ObjectId(key_id)})
    if not doc:
        return False, 0

    remaining = doc["credits_total"] - doc["credits_used"]
    if remaining < amount:
        logger.warning("Insufficient credits  key_id=%s  needed=%d  have=%d", key_id, amount, remaining)
        return False, remaining

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    month_str = now.strftime("%Y-%m")

    # Reset daily/monthly counters if new day/month
    reset_patch: dict[str, Any] = {}
    if doc.get("day_reset") != today_str:
        reset_patch["calls_today"] = 0
        reset_patch["day_reset"]   = today_str
    if doc.get("month_reset") != month_str:
        reset_patch["calls_this_month"] = 0
        reset_patch["month_reset"]      = month_str

    update: dict[str, Any] = {
        "$inc": {
            "credits_used":     amount,
            "calls_today":      1,
            "calls_this_month": 1,
        },
        "$set": {"last_used_at": now, **reset_patch},
    }
    col.update_one({"_id": ObjectId(key_id)}, update)

    new_remaining = remaining - amount
    logger.debug("Credits deducted  key_id=%s  amount=%d  remaining=%d", key_id, amount, new_remaining)
    return True, new_remaining


def get_usage_summary(key_id: str) -> dict[str, Any]:
    """Return usage stats for a given key_id."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    col = get_collection(Collections.API_KEYS)
    doc = col.find_one({"_id": ObjectId(key_id)})
    if not doc:
        return {}

    return {
        "key_id":              str(doc["_id"]),
        "owner_name":          doc["owner_name"],
        "credits_total":       doc["credits_total"],
        "credits_used":        doc["credits_used"],
        "credits_remaining":   doc["credits_total"] - doc["credits_used"],
        "calls_today":         doc.get("calls_today", 0),
        "calls_this_month":    doc.get("calls_this_month", 0),
        "status":              doc["status"],
        "created_at":          doc["created_at"],
        "last_used_at":        doc.get("last_used_at"),
        "ai_runs_this_month":   doc.get("ai_runs_this_month", 0),
        "ai_runs_monthly_quota": doc.get("ai_runs_monthly_quota"),
        "entitled_modules":     doc.get("entitled_modules"),
    }


def get_ai_runs_this_month(key_id: str) -> int:
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    col = get_collection(Collections.API_KEYS)
    doc = col.find_one({"_id": ObjectId(key_id)})
    if not doc:
        return 0
    reset_patch: dict[str, Any] = {}
    if doc.get("ai_runs_month_reset") != month_str:
        reset_patch["ai_runs_this_month"] = 0
        reset_patch["ai_runs_month_reset"] = month_str
    if reset_patch:
        col.update_one({"_id": ObjectId(key_id)}, {"$set": reset_patch})
        doc = col.find_one({"_id": ObjectId(key_id)})
    try:
        return int((doc or {}).get("ai_runs_this_month") or 0)
    except (TypeError, ValueError):
        return 0


def increment_ai_runs_this_month(key_id: str) -> int:
    """Increment AI-attributed run counter for the key's current month; returns new count."""
    from agency_api.database import get_collection, Collections
    from bson import ObjectId  # type: ignore

    _ = get_ai_runs_this_month(key_id)
    col = get_collection(Collections.API_KEYS)
    col.update_one({"_id": ObjectId(key_id)}, {"$inc": {"ai_runs_this_month": 1}})
    doc = col.find_one({"_id": ObjectId(key_id)})
    try:
        return int((doc or {}).get("ai_runs_this_month") or 0)
    except (TypeError, ValueError):
        return 0
