"""
Create / update ``public.cusear_users`` from Razorpay subscription webhooks or manual signup.

Requires Supabase service role (same as ``agency_api.supabase_client``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from agency_api.supabase_client import get_supabase

logger = logging.getLogger(__name__)

Outcome = Literal["created", "updated", "reactivated"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_agent_token() -> str:
    return str(uuid.uuid4())


def find_user_by_email(email: str) -> dict[str, Any] | None:
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = sb.table("cusear_users").select("*").eq("email", email.strip().lower()).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("find_user_by_email: %s", exc, exc_info=True)
        return None


def find_user_by_subscription_id(sub_id: str) -> dict[str, Any] | None:
    if not sub_id:
        return None
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = (
            sb.table("cusear_users")
            .select("*")
            .eq("razorpay_subscription_id", sub_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("find_user_by_subscription_id: %s", exc, exc_info=True)
        return None


def provision_cusear_user(
    *,
    email: str,
    phone: str | None,
    plan: str | None,
    razorpay_subscription_id: str | None,
    expires_at_iso: str | None,
    auth_user_id: str | None = None,
    active: bool = True,
) -> tuple[dict[str, Any], Outcome]:
    """
    Insert or update a product user row. Email is normalized lowercase.

    - If ``razorpay_subscription_id`` matches an existing row → update plan/active/dates (keep agent_token).
    - Else if email exists → update same fields (keep agent_token).
    - Else insert new row with fresh ``agent_token``.
    """
    sb = get_supabase()
    if not sb:
        raise RuntimeError("Supabase not configured")

    email_norm = email.strip().lower()
    if not email_norm:
        raise ValueError("email required")

    existing_sub = find_user_by_subscription_id(razorpay_subscription_id or "") if razorpay_subscription_id else None
    existing_email = find_user_by_email(email_norm)
    existing_auth = None
    if auth_user_id:
        try:
            r_auth = (
                sb.table("cusear_users")
                .select("*")
                .eq("auth_user_id", str(auth_user_id))
                .limit(1)
                .execute()
            )
            rows_auth = getattr(r_auth, "data", None) or []
            existing_auth = rows_auth[0] if rows_auth else None
        except Exception as exc:
            logger.error("find_user_by_auth_user_id: %s", exc, exc_info=True)
    existing = existing_sub or existing_auth or existing_email

    patch: dict[str, Any] = {
        "email": email_norm,
        "phone": phone,
        "plan": plan,
        "active": active,
        "updated_at": _now_iso(),
    }
    if razorpay_subscription_id:
        patch["razorpay_subscription_id"] = razorpay_subscription_id
    if expires_at_iso:
        patch["expires_at"] = expires_at_iso
    if auth_user_id:
        patch["auth_user_id"] = str(auth_user_id)
    if active and not existing:
        patch["subscribed_at"] = _now_iso()

    if existing:
        uid = str(existing["id"])
        try:
            sb.table("cusear_users").update(patch).eq("id", uid).execute()
        except Exception as exc:
            logger.error("provision update failed: %s", exc, exc_info=True)
            raise
        merged = {**existing, **patch}
        outcome: Outcome = "updated"
        if existing.get("active") is False and active:
            outcome = "reactivated"
        return merged, outcome

    token = generate_agent_token()
    insert_row: dict[str, Any] = {
        "email": email_norm,
        "phone": phone,
        "plan": plan,
        "agent_token": token,
        "auth_user_id": str(auth_user_id) if auth_user_id else None,
        "active": active,
        "subscribed_at": _now_iso() if active else None,
        "razorpay_subscription_id": razorpay_subscription_id,
        "expires_at": expires_at_iso,
    }
    try:
        res = sb.table("cusear_users").insert(insert_row).execute()
        rows = getattr(res, "data", None) or []
        row = rows[0] if rows else {}
        if not row.get("id"):
            refetched = find_user_by_email(email_norm)
            if refetched:
                return refetched, "created"
        return {**insert_row, **row}, "created"
    except Exception as exc:
        logger.error("provision insert failed: %s", exc, exc_info=True)
        raise


def set_subscription_inactive(razorpay_subscription_id: str) -> bool:
    sb = get_supabase()
    if not sb or not razorpay_subscription_id:
        return False
    try:
        sb.table("cusear_users").update(
            {"active": False, "updated_at": _now_iso()}
        ).eq("razorpay_subscription_id", razorpay_subscription_id).execute()
        return True
    except Exception as exc:
        logger.error("set_subscription_inactive: %s", exc, exc_info=True)
        return False
