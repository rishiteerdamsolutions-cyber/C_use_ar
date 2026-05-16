"""
Supabase (Postgres) — optional. When env is unset, cloud-agent features degrade gracefully.

Env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None


def supabase_configured() -> bool:
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return bool(url and key)


def get_supabase() -> Any | None:
    global _client
    if not supabase_configured():
        return None
    if _client is not None:
        return _client
    try:
        from supabase import create_client
    except ImportError:
        logger.warning("supabase package not installed")
        return None
    url = os.environ["SUPABASE_URL"].strip()
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
    _client = create_client(url, key)
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_user_by_agent_token(agent_token: str) -> dict[str, Any] | None:
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = (
            sb.table("cusear_users")
            .select("id,email,phone,agent_token,plan,auth_user_id,active")
            .eq("agent_token", agent_token)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("Supabase find_user_by_agent_token: %s", exc, exc_info=True)
        return None


def get_workflow_json(user_id: str, workflow_name: str) -> dict[str, Any] | None:
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = (
            sb.table("cusear_workflows")
            .select("id,workflow_json,platform")
            .eq("user_id", user_id)
            .eq("name", workflow_name)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        return rows[0].get("workflow_json") if isinstance(rows[0].get("workflow_json"), dict) else None
    except Exception as exc:
        logger.error("get_workflow_json: %s", exc, exc_info=True)
        return None


def get_workflow_for_run(user_id: str, workflow_id: str) -> dict[str, Any] | None:
    """
    Resolve a workflow row by id for Run Now.
    Returns {id, name, platform, workflow_json} or None.
    """
    sb = get_supabase()
    if not sb:
        return None
    try:
        res = (
            sb.table("cusear_workflows")
            .select("id,name,platform,workflow_json")
            .eq("user_id", user_id)
            .eq("id", workflow_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        wf = row.get("workflow_json")
        if not isinstance(wf, dict):
            return None
        return {
            "id": str(row.get("id")),
            "name": str(row.get("name") or row.get("id") or "workflow"),
            "platform": row.get("platform"),
            "workflow_json": wf,
        }
    except Exception as exc:
        logger.error("get_workflow_for_run: %s", exc, exc_info=True)
        return None


def upsert_workflow_json(
    *,
    user_id: str,
    workflow_name: str,
    workflow_json: dict[str, Any],
    platform: str | None = None,
) -> dict[str, Any] | None:
    """
    Upsert workflow by (user_id, name) and return row metadata.
    """
    sb = get_supabase()
    if not sb:
        return None
    row: dict[str, Any] = {
        "user_id": user_id,
        "name": workflow_name,
        "workflow_json": workflow_json,
        "updated_at": _now_iso(),
    }
    if platform is not None:
        row["platform"] = platform
    try:
        try:
            sb.table("cusear_workflows").upsert(
                row, on_conflict="user_id,name"
            ).execute()
        except TypeError:
            sb.table("cusear_workflows").upsert(row).execute()

        # Re-fetch to get canonical row id/name/platform.
        res = (
            sb.table("cusear_workflows")
            .select("id,name,platform,workflow_json")
            .eq("user_id", user_id)
            .eq("name", workflow_name)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("upsert_workflow_json: %s", exc, exc_info=True)
        return None


def list_schedules_for_user(user_id: str) -> list[dict[str, Any]]:
    sb = get_supabase()
    if not sb:
        return []
    try:
        res = (
            sb.table("cusear_schedules")
            .select("id,user_id,workflow_id,run_time,days,active,content_map,created_at,updated_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        return getattr(res, "data", None) or []
    except Exception as exc:
        logger.error("list_schedules_for_user: %s", exc, exc_info=True)
        return []


def upsert_schedule(
    *,
    user_id: str,
    workflow_id: str,
    run_time: str,
    days: list[str],
    content_map: dict[str, Any] | None = None,
    active: bool = True,
) -> dict[str, Any] | None:
    sb = get_supabase()
    if not sb:
        return None
    row: dict[str, Any] = {
        "user_id": user_id,
        "workflow_id": workflow_id,
        "run_time": run_time,
        "days": days,
        "active": active,
        "content_map": content_map or {},
        "updated_at": _now_iso(),
    }
    try:
        # create new schedule row
        res = sb.table("cusear_schedules").insert(row).execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("upsert_schedule: %s", exc, exc_info=True)
        return None


def list_active_schedules() -> list[dict[str, Any]]:
    """
    Read active schedules globally for scheduler polling loop.
    """
    sb = get_supabase()
    if not sb:
        return []
    try:
        res = (
            sb.table("cusear_schedules")
            .select("id,user_id,workflow_id,run_time,days,active,content_map")
            .eq("active", True)
            .execute()
        )
        return getattr(res, "data", None) or []
    except Exception as exc:
        logger.error("list_active_schedules: %s", exc, exc_info=True)
        return []


def upsert_agent_status(user_id: str, payload: dict[str, Any]) -> None:
    sb = get_supabase()
    if not sb:
        return
    row: dict[str, Any] = {
        "user_id": user_id,
        "connected": bool(payload.get("connected")),
        "os": payload.get("os"),
        "agent_version": payload.get("version") or payload.get("agent_version"),
        "last_seen": payload.get("last_seen") or _now_iso(),
        "updated_at": _now_iso(),
    }
    if "chrome" in payload:
        row["chrome_ok"] = bool(payload.get("chrome"))
    if payload.get("disk_free_mb") is not None:
        row["disk_free_mb"] = int(payload["disk_free_mb"])
    if payload.get("workflows") is not None:
        row["workflows"] = payload.get("workflows")
    try:
        sb.table("cusear_agent_status").upsert(row, on_conflict="user_id").execute()
    except TypeError:
        # Older supabase-py without on_conflict kwarg
        sb.table("cusear_agent_status").upsert(row).execute()
    except Exception as exc:
        logger.error("upsert_agent_status: %s", exc, exc_info=True)


def update_run_status(
    run_id: str,
    *,
    status: str,
    error: str | None = None,
    completed: bool = False,
) -> None:
    sb = get_supabase()
    if not sb:
        return
    patch: dict[str, Any] = {"status": status}
    if error is not None:
        patch["error"] = error
    if completed:
        patch["completed_at"] = _now_iso()
    try:
        sb.table("cusear_runs").update(patch).eq("id", run_id).execute()
    except Exception as exc:
        logger.error("update_run_status: %s", exc, exc_info=True)


def insert_run_row(
    *,
    run_id: str,
    user_id: str,
    workflow_name: str | None,
    status: str,
    report_secret: str,
    workflow_id: str | None = None,
) -> None:
    sb = get_supabase()
    if not sb:
        return
    row: dict[str, Any] = {
        "id": run_id,
        "user_id": user_id,
        "workflow_name": workflow_name,
        "status": status,
        "report_secret": report_secret,
        "started_at": _now_iso(),
    }
    if workflow_id:
        row["workflow_id"] = workflow_id
    try:
        sb.table("cusear_runs").insert(row).execute()
    except Exception as exc:
        logger.error("insert_run_row: %s", exc, exc_info=True)


def verify_dev_agent_token(token: str) -> dict[str, Any] | None:
    """When Supabase is off, allow CUSEAR_DEV_AGENT_TOKEN for local E2E."""
    expected = (os.environ.get("CUSEAR_DEV_AGENT_TOKEN") or "").strip()
    if expected and hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")):
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "email": "dev@local",
            "agent_token": token,
            "active": True,
        }
    return None
