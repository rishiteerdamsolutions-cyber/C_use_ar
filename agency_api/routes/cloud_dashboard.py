"""
Customer dashboard API (called from ``app.*`` static site on Vercel).

Prefix: ``/api/cloud/v1``
Auth: ``Authorization: Bearer <supabase_jwt>`` or ``X-Agent-Token: <agent_token>`` (see ``cloud_auth``).
"""

from __future__ import annotations

import logging
import os
import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, EmailStr, Field

from agency_api import agent_hub
from agency_api.cloud_auth import resolve_cloud_identity, resolve_cloud_user
from agency_api.email_resend import send_welcome_email
from agency_api.supabase_client import (
    get_supabase,
    get_workflow_for_run,
    get_workflow_json,
    insert_run_row,
    list_schedules_for_user,
    supabase_configured,
    upsert_schedule,
    upsert_workflow_json,
)
from agency_api.user_provisioning import find_user_by_email, provision_cusear_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cloud/v1", tags=["Cloud Dashboard"])


class RunNowRequest(BaseModel):
    workflow_id: str = Field(..., min_length=1)
    platform: str | None = None
    content_map: dict[str, Any] = Field(default_factory=dict)


class ManualSignupRequest(BaseModel):
    """Testing-only signup when payments are not wired. Guard with ``X-Cloud-Signup-Secret``."""

    email: EmailStr
    phone: str | None = None
    plan: str | None = "testing"


class CreateWorkflowRequest(BaseModel):
    workflow_name: str = Field(..., min_length=1)
    workflow_json: dict[str, Any] = Field(default_factory=dict)
    platform: str | None = None
    sync_to_agent: bool = True


class ScheduleCreateRequest(BaseModel):
    workflow_id: str = Field(..., min_length=1)
    run_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    days: list[str] = Field(default_factory=list)
    content_map: dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class CreateCheckoutRequest(BaseModel):
    plan: str = Field(..., min_length=1)
    success_url: str | None = None
    cancel_url: str | None = None


class ProductImportRequest(BaseModel):
    package: dict[str, Any] = Field(default_factory=dict)
    sync_to_agent: bool = True


@router.get("/health")
async def cloud_health() -> dict[str, Any]:
    return {
        "ok": True,
        "supabase": supabase_configured(),
        "public_api_base": (os.environ.get("PUBLIC_API_BASE_URL") or "").strip(),
    }


_PLAN_AMOUNTS_INR: dict[str, int] = {
    "core": 900,
    "hybrid": 1800,
    "ai_budget": 6600,
    "ai_pro": 16500,
}


def _normalize_plan(raw: str) -> str | None:
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "core": "core",
        "hybrid": "hybrid",
        "aibudget": "ai_budget",
        "budget": "ai_budget",
        "ai_budget": "ai_budget",
        "aipro": "ai_pro",
        "pro": "ai_pro",
        "ai_pro": "ai_pro",
    }
    return aliases.get(s)


@router.post("/payments/create-checkout")
async def create_checkout(
    body: CreateCheckoutRequest,
    identity: dict[str, Any] = Depends(resolve_cloud_identity),
) -> dict[str, Any]:
    """
    Auth-first checkout: requires valid Supabase login before payment creation.
    """
    plan = _normalize_plan(body.plan)
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid plan")

    amount_inr = _PLAN_AMOUNTS_INR.get(plan)
    if not amount_inr:
        raise HTTPException(status_code=400, detail="Unsupported plan pricing")

    key_id = (os.environ.get("RAZORPAY_KEY_ID") or "").strip()
    key_secret = (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
    if not key_id or not key_secret:
        raise HTTPException(status_code=503, detail="Razorpay not configured")

    auth_user_id = str(identity["auth_user_id"])
    email = str(identity["email"]).strip().lower()

    # Pre-provision inactive row so webhook has stable user mapping.
    provision_cusear_user(
        email=email,
        phone=None,
        plan=plan,
        razorpay_subscription_id=None,
        expires_at_iso=None,
        auth_user_id=auth_user_id,
        active=False,
    )

    try:
        import razorpay  # type: ignore
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="razorpay package missing on server") from exc

    client = razorpay.Client(auth=(key_id, key_secret))
    base = (os.environ.get("PUBLIC_APP_BASE_URL") or "").rstrip("/")
    callback_url = (body.success_url or "").strip() or (f"{base}/app" if base else None)
    link_data: dict[str, Any] = {
        "amount": amount_inr * 100,
        "currency": "INR",
        "accept_partial": False,
        "description": f"Cusear {plan} monthly plan",
        "customer": {"email": email},
        "notify": {"sms": False, "email": True},
        "notes": {
            "email": email,
            "plan": plan,
            "auth_user_id": auth_user_id,
            "source": "cloud_checkout",
        },
        "reference_id": f"cusear_{plan}_{auth_user_id[:8]}",
    }
    if callback_url:
        link_data["callback_url"] = callback_url
        link_data["callback_method"] = "get"

    try:
        payment_link = client.payment_link.create(link_data)
    except Exception as exc:
        logger.error("create-checkout failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create checkout link") from exc

    return {
        "ok": True,
        "plan": plan,
        "amount_inr": amount_inr,
        "checkout_url": payment_link.get("short_url") or payment_link.get("url"),
        "payment_link_id": payment_link.get("id"),
    }


@router.post("/auth/signup")
async def cloud_manual_signup(
    body: ManualSignupRequest,
    x_cloud_signup_secret: str | None = Header(default=None, alias="X-Cloud-Signup-Secret"),
) -> dict[str, Any]:
    """
    Create a ``cusear_users`` row without Razorpay (QA / staging).

    Requires env ``CUSEAR_CLOUD_SIGNUP_SECRET`` and matching header ``X-Cloud-Signup-Secret``.
    Do **not** expose this in production without a strong secret or remove the route.
    """
    expected = (os.environ.get("CUSEAR_CLOUD_SIGNUP_SECRET") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CUSEAR_CLOUD_SIGNUP_SECRET not configured",
        )
    if not x_cloud_signup_secret or x_cloud_signup_secret.strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid X-Cloud-Signup-Secret")

    if not supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured on server (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY).",
        )

    email_norm = body.email.strip().lower()
    if find_user_by_email(email_norm):
        raise HTTPException(status_code=409, detail="Email already registered")

    row, outcome = provision_cusear_user(
        email=email_norm,
        phone=body.phone,
        plan=body.plan,
        razorpay_subscription_id=None,
        expires_at_iso=None,
        active=True,
    )

    token = row.get("agent_token")
    if isinstance(token, str) and token:
        send_welcome_email(to_email=email_norm, agent_token=token, plan=body.plan)

    return {
        "ok": True,
        "outcome": outcome,
        "user_id": row.get("id"),
        "email": row.get("email"),
        "agent_token": token,
    }


@router.get("/me")
async def cloud_me(user: dict[str, Any] = Depends(resolve_cloud_user)) -> dict[str, Any]:
    """Account profile for the PWA (includes agent_token for local connector setup)."""
    uid = str(user["id"])
    return {
        "ok": True,
        "user_id": uid,
        "email": user.get("email"),
        "plan": user.get("plan"),
        "active": user.get("active", True),
        "agent_token": user.get("agent_token"),
        "agent_connected": agent_hub.is_agent_connected(uid),
    }


@router.get("/agent/status")
async def agent_status(user: dict[str, Any] = Depends(resolve_cloud_user)) -> dict[str, Any]:
    uid = str(user["id"])
    connected = agent_hub.is_agent_connected(uid)
    return {"connected": connected, "user_id": uid}


@router.post("/runs/run-now")
async def run_now(
    body: RunNowRequest,
    user: dict[str, Any] = Depends(resolve_cloud_user),
) -> dict[str, Any]:
    if not supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured on server (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY).",
        )

    uid = str(user["id"])
    wf_row = get_workflow_for_run(uid, body.workflow_id)
    if wf_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{body.workflow_id}' not found for user.",
        )
    wf = wf_row["workflow_json"]
    workflow_name = str(wf_row["name"])

    run_id = agent_hub.new_run_id()
    report_secret = agent_hub.new_report_secret()
    agent_hub.register_run(run_id, uid, report_secret)

    insert_run_row(
        run_id=run_id,
        user_id=uid,
        workflow_name=workflow_name,
        status="queued",
        report_secret=report_secret,
        workflow_id=body.workflow_id,
    )

    base = (os.environ.get("PUBLIC_API_BASE_URL") or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="PUBLIC_API_BASE_URL not set (e.g. https://api.cusear.autos).",
        )

    from urllib.parse import quote

    company_endpoint = f"{base}/agent/report/{run_id}?s={quote(report_secret, safe='')}"

    instruction: dict[str, Any] = {
        "type": "run_workflow",
        "run_id": run_id,
        "report_secret": report_secret,
        "workflow_name": workflow_name,
        "platform": body.platform or wf_row.get("platform"),
        "content_map": body.content_map,
        "workflow_data": wf,
        "company_endpoint": company_endpoint,
    }

    sent = await agent_hub.send_to_agent(uid, instruction)
    return {"run_id": run_id, "status": "sent" if sent else "queued"}


@router.get("/runs/recent")
async def runs_recent(
    user: dict[str, Any] = Depends(resolve_cloud_user),
    limit: int = 20,
) -> dict[str, Any]:
    sb = get_supabase()
    if not sb:
        return {"runs": []}
    uid = str(user["id"])
    try:
        q = (
            sb.table("cusear_runs")
            .select("id,workflow_name,status,error,started_at,completed_at")
            .eq("user_id", uid)
            .order("created_at", desc=True)
        )
        res = q.limit(min(limit, 100)).execute()
        return {"runs": getattr(res, "data", None) or []}
    except Exception as exc:
        logger.error("runs_recent: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list runs") from exc


@router.get("/workflows")
async def list_workflows(user: dict[str, Any] = Depends(resolve_cloud_user)) -> dict[str, Any]:
    sb = get_supabase()
    if not sb:
        return {"workflows": []}
    uid = str(user["id"])
    try:
        res = (
            sb.table("cusear_workflows")
            .select("id,name,platform,created_at,updated_at")
            .eq("user_id", uid)
            .order("updated_at", desc=True)
            .limit(200)
            .execute()
        )
        return {"workflows": getattr(res, "data", None) or []}
    except Exception as exc:
        logger.error("list_workflows: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list workflows") from exc


@router.post("/workflows/sync")
async def sync_workflow_to_agent(
    workflow_name: str = Query(..., description="Workflow name"),
    user: dict[str, Any] = Depends(resolve_cloud_user),
) -> dict[str, Any]:
    uid = str(user["id"])
    wf = get_workflow_json(uid, workflow_name)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    instruction = {
        "type": "sync_workflow",
        "workflow_name": workflow_name,
        "workflow_data": wf,
    }
    sent = await agent_hub.send_to_agent(uid, instruction)
    return {"status": "synced" if sent else "queued"}


@router.post("/workflows/upload")
async def upload_workflow(
    file: UploadFile = File(...),
    workflow_name: str | None = Form(default=None),
    platform: str | None = Form(default=None),
    user: dict[str, Any] = Depends(resolve_cloud_user),
) -> dict[str, Any]:
    """
    Upload workflow JSON from browser, store in Supabase, and sync to local agent.
    """
    if not supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured on server (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY).",
        )
    uid = str(user["id"])

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Workflow JSON must be an object")

    inferred_name = (
        (workflow_name or "").strip()
        or str(parsed.get("workflow_name") or "").strip()
        or os.path.splitext(os.path.basename(file.filename or "workflow"))[0]
    )
    if not inferred_name:
        raise HTTPException(status_code=400, detail="Could not determine workflow name")

    row = upsert_workflow_json(
        user_id=uid,
        workflow_name=inferred_name,
        workflow_json=parsed,
        platform=(platform or None),
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to store workflow")

    instruction = {
        "type": "sync_workflow",
        "workflow_name": str(row.get("name") or inferred_name),
        "workflow_data": parsed,
    }
    sent = await agent_hub.send_to_agent(uid, instruction)

    return {
        "ok": True,
        "workflow_id": row.get("id"),
        "workflow_name": row.get("name") or inferred_name,
        "sync_status": "synced" if sent else "queued",
    }


@router.post("/workflows/create")
async def create_workflow(
    body: CreateWorkflowRequest,
    user: dict[str, Any] = Depends(resolve_cloud_user),
) -> dict[str, Any]:
    """
    One-click workflow creation without manual SQL.
    Stores row in Supabase and optionally syncs to connected agent.
    """
    if not supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured on server (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY).",
        )
    uid = str(user["id"])
    if not isinstance(body.workflow_json, dict) or not body.workflow_json:
        raise HTTPException(status_code=400, detail="workflow_json must be a non-empty JSON object")

    row = upsert_workflow_json(
        user_id=uid,
        workflow_name=body.workflow_name.strip(),
        workflow_json=body.workflow_json,
        platform=body.platform,
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create workflow")

    sent = False
    if body.sync_to_agent:
        instruction = {
            "type": "sync_workflow",
            "workflow_name": str(row.get("name") or body.workflow_name),
            "workflow_data": body.workflow_json,
        }
        sent = await agent_hub.send_to_agent(uid, instruction)

    return {
        "ok": True,
        "workflow_id": row.get("id"),
        "workflow_name": row.get("name") or body.workflow_name,
        "sync_status": "synced" if sent else ("queued" if body.sync_to_agent else "not_requested"),
    }


@router.post("/products/import")
async def import_product_package(
    body: ProductImportRequest,
    user: dict[str, Any] = Depends(resolve_cloud_user),
) -> dict[str, Any]:
    """
    Import a trainer-exported AR product package and optionally sync to lightweight agent.
    """
    uid = str(user["id"])
    package = body.package if isinstance(body.package, dict) else {}
    if package.get("kind") != "cusear_ar_product":
        raise HTTPException(status_code=400, detail="Invalid package kind (expected cusear_ar_product)")

    workflow = package.get("workflow")
    if not isinstance(workflow, dict):
        raise HTTPException(status_code=400, detail="Invalid package: missing workflow object")
    workflow_name = str(workflow.get("workflow_name") or "").strip()
    workflow_json = workflow.get("workflow_json")
    if not workflow_name or not isinstance(workflow_json, dict):
        raise HTTPException(status_code=400, detail="Invalid package workflow payload")

    product_meta = package.get("product")
    platform = None
    if isinstance(product_meta, dict):
        plan = str(product_meta.get("plan") or "").strip()
        if plan:
            workflow_json = {**workflow_json, "cusear_product_plan": plan}

    row = upsert_workflow_json(
        user_id=uid,
        workflow_name=workflow_name,
        workflow_json=workflow_json,
        platform=platform,
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to store imported product workflow")

    sent = False
    if body.sync_to_agent:
        instruction = {
            "type": "sync_product",
            "product_package": package,
        }
        sent = await agent_hub.send_to_agent(uid, instruction)
        if not sent:
            # Fallback to legacy sync_workflow semantics.
            legacy = {
                "type": "sync_workflow",
                "workflow_name": workflow_name,
                "workflow_data": workflow_json,
            }
            sent = await agent_hub.send_to_agent(uid, legacy)

    return {
        "ok": True,
        "workflow_id": row.get("id"),
        "workflow_name": row.get("name") or workflow_name,
        "sync_status": "synced" if sent else ("queued" if body.sync_to_agent else "not_requested"),
    }


@router.get("/schedules")
async def list_schedules(user: dict[str, Any] = Depends(resolve_cloud_user)) -> dict[str, Any]:
    uid = str(user["id"])
    return {"schedules": list_schedules_for_user(uid)}


@router.post("/schedules/create")
async def create_schedule(
    body: ScheduleCreateRequest,
    user: dict[str, Any] = Depends(resolve_cloud_user),
) -> dict[str, Any]:
    if not supabase_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured on server (set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY).",
        )
    uid = str(user["id"])
    wf_row = get_workflow_for_run(uid, body.workflow_id)
    if not wf_row:
        raise HTTPException(status_code=404, detail="Workflow not found for this user")

    valid_days = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    days = [d for d in body.days if d in valid_days]
    if not days:
        raise HTTPException(status_code=400, detail="Select at least one day")

    row = upsert_schedule(
        user_id=uid,
        workflow_id=body.workflow_id,
        run_time=body.run_time,
        days=days,
        content_map=body.content_map,
        active=body.active,
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create schedule")

    return {
        "ok": True,
        "schedule_id": row.get("id"),
        "workflow_id": row.get("workflow_id"),
        "run_time": row.get("run_time"),
        "days": row.get("days"),
        "active": row.get("active"),
    }
