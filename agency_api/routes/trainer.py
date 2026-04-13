"""
Cloud trainer API — same paths as dashboard.py under prefix /api/trainer.
Workflows stored in MongoDB, scoped by X-API-Key (Mongo key _id) when provided.
Set TRAINER_REQUIRE_API_KEY=1 so only authenticated keys can use the trainer.
Live desktop run is only on python3 dashboard.py.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any, Dict, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from agency_api.keys import validate_key
from agency_api.trainer_service import ANONYMOUS_OWNER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trainer", tags=["Trainer (cloud)"])


def _require_trainer_api_key() -> bool:
    raw = os.environ.get("TRAINER_REQUIRE_API_KEY", "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes")
    # Safer default in production deployments.
    return os.environ.get("APP_MODE", "production").strip().lower() != "development"


async def resolve_trainer_owner(
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> str:
    require = _require_trainer_api_key()
    raw = (x_api_key or "").strip()
    if require and not raw:
        raise HTTPException(
            status_code=401,
            detail="X-API-Key required — create a key via the platform API and paste it in the trainer.",
        )
    if not raw:
        return ANONYMOUS_OWNER
    doc = validate_key(raw)
    if not doc:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return str(doc["_id"])


TrainerOwner = Annotated[str, Depends(resolve_trainer_owner)]


def _mongo_error(exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": f"Database unavailable: {exc}"},
    )


@router.get("/health")
async def trainer_health() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/workflows")
async def trainer_list_workflows(owner_id: TrainerOwner) -> Any:
    try:
        from agency_api.trainer_service import list_workflows

        return {"workflows": list_workflows(owner_id)}
    except Exception as exc:
        logger.warning("trainer list_workflows: %s", exc)
        return JSONResponse(status_code=503, content={"workflows": [], "error": str(exc)})


@router.get("/workflow/{wf_name}")
async def trainer_get_workflow(wf_name: str, owner_id: TrainerOwner) -> Any:
    name = unquote(wf_name)
    try:
        from agency_api.trainer_service import get_workflow

        wf = get_workflow(owner_id, name)
        if not wf:
            return JSONResponse(status_code=404, content={"error": "not found"})
        return wf
    except Exception as exc:
        return _mongo_error(exc)


@router.delete("/workflow/{wf_name}")
async def trainer_delete_workflow(wf_name: str, owner_id: TrainerOwner) -> Any:
    name = unquote(wf_name)
    try:
        from agency_api.trainer_service import delete_workflow

        if delete_workflow(owner_id, name):
            return {"deleted": True}
        return JSONResponse(status_code=404, content={"error": "not found"})
    except Exception as exc:
        return _mongo_error(exc)


@router.delete("/workflow/{wf_name}/step/{step_num:int}")
async def trainer_delete_step(wf_name: str, step_num: int, owner_id: TrainerOwner) -> Any:
    name = unquote(wf_name)
    try:
        from agency_api.trainer_service import delete_step

        ok, n = delete_step(owner_id, name, step_num)
        if not ok:
            return JSONResponse(status_code=404, content={"error": "not found"})
        return {"deleted": True, "total_steps": n}
    except Exception as exc:
        return _mongo_error(exc)


@router.post("/teach/step")
async def trainer_teach_step(request: Request, owner_id: TrainerOwner) -> Any:
    try:
        from agency_api.trainer_service import process_teach_step

        body = await request.body()
        ct = request.headers.get("Content-Type", "")
        out = process_teach_step(owner_id, body, ct)
        return out
    except ValueError as ve:
        return JSONResponse(status_code=400, content={"error": str(ve)})
    except Exception as exc:
        logger.exception("trainer teach/step")
        return JSONResponse(status_code=500, content={"error": "teach step failed"})


@router.post("/run")
async def trainer_run(request: Request, owner_id: TrainerOwner) -> Any:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    name = (data.get("workflow_name") or "").strip()
    dry_run = bool(data.get("dry_run", False))
    mode = str(data.get("mode") or "smart").strip().lower()
    if not name:
        return JSONResponse(status_code=400, content={"error": "workflow_name required"})

    if not dry_run:
        return JSONResponse(
            status_code=501,
            content={
                "success": False,
                "error": (
                    "Live desktop execution is not available in the cloud. "
                    "Use Dry run here to validate steps, or run `python3 dashboard.py` on your machine "
                    "with this client's credentials in the OS keychain / env, and sync workflows from Mongo "
                    "or recreate the workflow name under workflows/ locally."
                ),
            },
        )

    try:
        from agency_api.trainer_service import run_dry

        results = run_dry(owner_id, name)
        return {"success": True, "mode": mode, "steps": results}
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as exc:
        logger.exception("trainer run")
        return JSONResponse(status_code=500, content={"error": "trainer run failed"})
