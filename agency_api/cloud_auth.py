"""Resolve dashboard / cloud API callers (Supabase JWT or agent token header)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Header, HTTPException

from agency_api.supabase_client import find_user_by_agent_token, get_supabase, supabase_configured

logger = logging.getLogger(__name__)


def _require_active(user: dict[str, Any]) -> None:
    if user.get("active") is False:
        raise HTTPException(status_code=403, detail="Subscription inactive")


async def resolve_cloud_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
) -> dict[str, Any]:
    """
    Prefer ``Authorization: Bearer <supabase_access_token>``.
    Fallback (dev / simple hosting): ``X-Agent-Token: <agent_token>`` (same secret the agent uses).
    """
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        user = _user_from_supabase_jwt(token)
        if user:
            _require_active(user)
            return user
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if x_agent_token:
        u = find_user_by_agent_token(x_agent_token.strip())
        if u:
            _require_active(u)
            return u
        raise HTTPException(status_code=401, detail="Invalid agent token")

    raise HTTPException(status_code=401, detail="Missing Authorization or X-Agent-Token")


async def resolve_cloud_identity(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """
    Resolve logged-in identity from Supabase JWT without requiring active subscription.
    Use for pre-payment actions (checkout creation).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization bearer token")
    token = authorization.split(" ", 1)[1].strip()
    auth_user = _auth_user_from_supabase_jwt(token)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    email = str(auth_user.get("email") or "").strip().lower()
    uid = str(auth_user.get("id") or "").strip()
    if not uid or not email:
        raise HTTPException(status_code=401, detail="Session missing user identity")
    return {"auth_user_id": uid, "email": email}


def _user_from_supabase_jwt(access_token: str) -> dict[str, Any] | None:
    if not supabase_configured():
        return None
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = sb.auth.get_user(access_token)
        auth_user = getattr(res, "user", None)
        if auth_user is None:
            return None
        uid = str(auth_user.id)
        r = (
            sb.table("cusear_users")
            .select("id,email,phone,agent_token,plan,auth_user_id,active")
            .eq("auth_user_id", uid)
            .limit(1)
            .execute()
        )
        rows = getattr(r, "data", None) or []
        if rows:
            return rows[0]
        # JWT valid but no product row yet
        return None
    except Exception as exc:
        logger.warning("JWT validation failed: %s", exc)
        return None


def _auth_user_from_supabase_jwt(access_token: str) -> dict[str, Any] | None:
    if not supabase_configured():
        return None
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = sb.auth.get_user(access_token)
        auth_user = getattr(res, "user", None)
        if auth_user is None:
            return None
        return {"id": str(auth_user.id), "email": str(getattr(auth_user, "email", "") or "")}
    except Exception as exc:
        logger.warning("JWT validation failed: %s", exc)
        return None
