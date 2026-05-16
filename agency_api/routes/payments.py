"""
Razorpay — subscription webhook and user provisioning (Supabase ``cusear_users``).

Mount at ``POST /webhook/razorpay`` (configure this URL in the Razorpay dashboard).

Place **email** (and optional **phone**, **plan**) in subscription ``notes`` when creating
the subscription in Razorpay so webhooks can provision accounts without extra API calls.

Handled events:
  - ``subscription.activated`` — create/update user, welcome email on first create
  - ``subscription.charged`` — refresh ``expires_at`` from Razorpay ``current_end``
  - ``subscription.cancelled`` / ``subscription.completed`` / ``subscription.paused`` — ``active=false``

Uses the same ``X-Razorpay-Signature`` + ``RAZORPAY_WEBHOOK_SECRET`` as wallet webhooks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from agency_api.billing import verify_razorpay_webhook_signature
from agency_api.email_resend import send_welcome_email
from agency_api.supabase_client import get_supabase, supabase_configured
from agency_api.user_provisioning import (
    find_user_by_email,
    provision_cusear_user,
    set_subscription_inactive,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Payments — Razorpay"])


def _expires_iso_from_entity(ent: dict[str, Any]) -> str | None:
    ce = ent.get("current_end")
    if ce is None:
        return None
    try:
        ts = int(ce)
        if ts > 10**12:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _subscription_entity(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("payload") or {}
    sub = payload.get("subscription")
    if isinstance(sub, dict):
        ent = sub.get("entity")
        if isinstance(ent, dict):
            return ent
        # Sometimes payload nests differently
        if "id" in sub and "status" in sub:
            return sub
    return {}


def _payment_entity(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("payload") or {}
    pay = payload.get("payment")
    if isinstance(pay, dict):
        ent = pay.get("entity")
        if isinstance(ent, dict):
            return ent
    return {}


def _notes_dict(ent: dict[str, Any]) -> dict[str, Any]:
    notes = ent.get("notes")
    if isinstance(notes, dict):
        return notes
    if isinstance(notes, str) and notes.strip():
        try:
            parsed = json.loads(notes)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _email_phone_plan(ent: dict[str, Any]) -> tuple[str, str | None, str | None]:
    notes = _notes_dict(ent)
    email = (
        (notes.get("email") or notes.get("user_email") or ent.get("customer_email") or "")
        .strip()
        .lower()
    )
    phone = notes.get("phone")
    if phone is not None:
        phone = str(phone).strip() or None
    plan = notes.get("plan") or ent.get("plan_id")
    if plan is not None:
        plan = str(plan).strip()
    return email, phone, plan


@router.post("/webhook/razorpay", include_in_schema=False)
async def razorpay_control_plane_webhook(request: Request) -> dict[str, Any]:
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_razorpay_webhook_signature(raw, signature):
        raise HTTPException(status_code=400, detail="invalid_signature")

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_json")

    if not supabase_configured():
        logger.warning("Razorpay webhook: Supabase not configured — event=%s", body.get("event"))
        return {"ok": True, "reason": "supabase_not_configured"}

    event = (body.get("event") or "").strip()
    logger.info("Razorpay webhook event=%s", event)

    if event == "subscription.activated":
        return _handle_subscription_activated(body)
    if event == "subscription.charged":
        return _handle_subscription_charged(body)
    if event in ("subscription.cancelled", "subscription.completed", "subscription.paused"):
        return _handle_subscription_ended(body)
    if event == "payment.captured":
        return _handle_payment_captured(body)

    return {"ok": True, "reason": f"ignored:{event}"}


def _handle_subscription_activated(body: dict[str, Any]) -> dict[str, Any]:
    ent = _subscription_entity(body)
    sub_id = str(ent.get("id") or "")
    email, phone, plan = _email_phone_plan(ent)
    auth_user_id = str(_notes_dict(ent).get("auth_user_id") or "").strip() or None
    if not email:
        logger.error("subscription.activated: missing email in notes / payload")
        return {"ok": False, "reason": "missing_email"}

    expires_at = _expires_iso_from_entity(ent)
    row, outcome = provision_cusear_user(
        email=email,
        phone=phone,
        plan=plan,
        razorpay_subscription_id=sub_id or None,
        expires_at_iso=expires_at,
        auth_user_id=auth_user_id,
        active=True,
    )

    if outcome == "created":
        tok = row.get("agent_token")
        if isinstance(tok, str) and tok:
            send_welcome_email(to_email=email, agent_token=tok, plan=plan)

    return {"ok": True, "outcome": outcome, "user_id": row.get("id")}


def _handle_subscription_charged(body: dict[str, Any]) -> dict[str, Any]:
    """Renewal — refresh expiry and keep active."""
    ent = _subscription_entity(body)
    sub_id = str(ent.get("id") or "")
    email, phone, plan = _email_phone_plan(ent)
    auth_user_id = str(_notes_dict(ent).get("auth_user_id") or "").strip() or None
    if not email:
        logger.warning("subscription.charged: missing email — skipping provision")
        return {"ok": True, "reason": "missing_email"}

    expires_at = _expires_iso_from_entity(ent)
    row, outcome = provision_cusear_user(
        email=email,
        phone=phone,
        plan=plan,
        razorpay_subscription_id=sub_id or None,
        expires_at_iso=expires_at,
        auth_user_id=auth_user_id,
        active=True,
    )
    return {"ok": True, "outcome": outcome, "user_id": row.get("id")}


def _handle_subscription_ended(body: dict[str, Any]) -> dict[str, Any]:
    ent = _subscription_entity(body)
    sub_id = str(ent.get("id") or "")
    if sub_id:
        set_subscription_inactive(sub_id)
        return {"ok": True, "deactivated": True, "subscription_id": sub_id}

    email, _, _ = _email_phone_plan(ent)
    if email:
        u = find_user_by_email(email)
        if u:
            sb = get_supabase()
            if sb:
                sb.table("cusear_users").update(
                    {"active": False, "updated_at": datetime.now(timezone.utc).isoformat()}
                ).eq("id", str(u["id"])).execute()
            return {"ok": True, "deactivated": True, "user_id": u.get("id")}

    return {"ok": True, "reason": "no_subscription_id"}


def _handle_payment_captured(body: dict[str, Any]) -> dict[str, Any]:
    """
    Auth-first checkout flow via Razorpay payment links (non-subscription event).
    """
    ent = _payment_entity(body)
    notes = _notes_dict(ent)
    email = str(notes.get("email") or ent.get("email") or "").strip().lower()
    if not email:
        logger.warning("payment.captured: missing email in notes/entity")
        return {"ok": False, "reason": "missing_email"}
    plan = str(notes.get("plan") or "").strip() or None
    auth_user_id = str(notes.get("auth_user_id") or "").strip() or None
    # Default monthly entitlement window for one-time plan payment links.
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    row, outcome = provision_cusear_user(
        email=email,
        phone=None,
        plan=plan,
        razorpay_subscription_id=None,
        expires_at_iso=expires_at,
        auth_user_id=auth_user_id,
        active=True,
    )
    if outcome == "created":
        tok = row.get("agent_token")
        if isinstance(tok, str) and tok:
            send_welcome_email(to_email=email, agent_token=tok, plan=plan)
    return {
        "ok": True,
        "outcome": outcome,
        "user_id": row.get("id"),
        "entitlement_days": 30,
    }
