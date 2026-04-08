"""
Routes — /api/v1/workflows/*
Run saved workflows and teach new ones via screenshots.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from api.middleware import assert_credits, require_api_key
from api.models import (
    ENDPOINT_COSTS,
    RunMode,
    RunWorkflowRequest,
    RunWorkflowResponse,
    TeachRequest,
    TeachResponse,
)

router = APIRouter(prefix="/workflows", tags=["Workflows"])
logger = logging.getLogger(__name__)


# ─── Run Workflow ─────────────────────────────────────────────────────────────
@router.post("/run", response_model=RunWorkflowResponse,
    summary="Run a saved workflow",
    description=(
        "Execute a taught workflow by name.\n\n"
        "**fast mode** (V1) — Uses saved training coordinates. Zero vision API calls. 2 credits.\n\n"
        "**smart mode** (V2) — Claude Vision re-checks every element live. Adapts to UI changes. 5 credits."
    ),
)
async def run_workflow(
    req:     RunWorkflowRequest,
    key_doc: dict = Depends(require_api_key),
) -> dict[str, Any]:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    cost_key = f"run_workflow_{req.mode.value}"
    cost     = ENDPOINT_COSTS[cost_key]
    key_id   = str(key_doc["_id"])

    assert_credits(key_doc, cost)

    t0 = time.time()
    success      = False
    session_id   = ""
    live_url     = ""
    steps_run    = 0
    success_rate = 0.0

    try:
        if req.mode == RunMode.fast:
            from teach.runner_v1 import RunnerV1
            runner = RunnerV1(req.workflow_name, dry_run=False)
        else:
            from teach.workflow_runner import WorkflowRunner
            runner = WorkflowRunner(req.workflow_name, dry_run=False)

        ok       = runner.run(variables=req.variables)
        success  = ok
        steps_run    = runner._workflow.get("total_steps", 0)
        success_rate = 1.0 if ok else 0.5

    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Workflow run failed: %s", exc, exc_info=True)
        success = False

    duration = time.time() - t0

    # Deduct credits and log
    from api.keys import deduct_credits
    from api.usage import log_call
    _, remaining = deduct_credits(key_id, cost)
    log_call(
        key_id=key_id,
        endpoint="run_workflow",
        mode=req.mode.value,
        credits_used=cost,
        credits_remaining=remaining,
        duration_s=duration,
        success=success,
        metadata={"workflow_name": req.workflow_name, "live_url": live_url},
    )

    return {
        "workflow_name":     req.workflow_name,
        "mode":              req.mode,
        "success":           success,
        "session_id":        session_id or "N/A",
        "live_url":          live_url or None,
        "steps_run":         steps_run,
        "success_rate":      success_rate,
        "duration_s":        round(duration, 2),
        "credits_used":      cost,
        "credits_remaining": remaining,
    }


# ─── Teach Workflow ───────────────────────────────────────────────────────────
@router.post("/teach", response_model=TeachResponse,
    summary="Teach a new workflow from screenshots",
    description=(
        "Upload numbered screenshots (1.png, 2.png…) with step instructions.\n"
        "Claude Vision analyses each screenshot and returns a replayable workflow JSON.\n\n"
        "**Cost:** 3 credits per screenshot."
    ),
)
async def teach_workflow(
    workflow_name: str        = Form(...),
    instructions:  str        = Form(..., description='JSON array: ["Click Deploy","Type name",...]'),
    screenshots:   list[UploadFile] = File(...),
    key_doc:       dict       = Depends(require_api_key),
) -> dict[str, Any]:
    import json as _json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    key_id     = str(key_doc["_id"])
    num_shots  = len(screenshots)
    cost       = ENDPOINT_COSTS["teach"] * num_shots

    assert_credits(key_doc, cost)

    try:
        instr_list: list[str] = _json.loads(instructions)
    except Exception:
        raise HTTPException(status_code=400, detail="instructions must be a valid JSON array of strings")

    if len(instr_list) != num_shots:
        raise HTTPException(
            status_code=400,
            detail=f"Got {num_shots} screenshots but {len(instr_list)} instructions — must match.",
        )

    t0 = time.time()
    success = False
    workflow_result: dict[str, Any] = {}

    try:
        import tempfile
        from teach.screenshot_teacher import teach_from_screenshots

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            steps = []
            for i, (upload, instr) in enumerate(zip(screenshots, instr_list), start=1):
                content  = await upload.read()
                ext      = Path(upload.filename or "img.png").suffix or ".png"
                img_path = tmp / f"{i}{ext}"
                img_path.write_bytes(content)
                steps.append({"screenshot": str(img_path), "instruction": instr})

            workflow_result = teach_from_screenshots(
                workflow_name=workflow_name,
                steps=steps,
                screenshot_dir=tmp,
            )
            success = True

    except Exception as exc:
        logger.error("Teach failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    duration = time.time() - t0

    from api.keys import deduct_credits
    from api.usage import log_call
    _, remaining = deduct_credits(key_id, cost)
    log_call(
        key_id=key_id,
        endpoint="teach",
        mode=None,
        credits_used=cost,
        credits_remaining=remaining,
        duration_s=duration,
        success=success,
        metadata={"workflow_name": workflow_name, "screenshots": num_shots},
    )

    return {
        "workflow_name":     workflow_name,
        "total_steps":       num_shots,
        "workflow_json":     workflow_result,
        "credits_used":      cost,
        "credits_remaining": remaining,
    }


# ─── List workflows ───────────────────────────────────────────────────────────
@router.get("/list",
    summary="List all available workflows",
)
async def list_workflows(key_doc: dict = Depends(require_api_key)) -> dict[str, Any]:
    from teach.workflow_runner import list_workflows as _list
    return {"workflows": _list()}
