"""
Routes — /api/v1/templates/*
List templates and trigger full website builds.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.middleware import assert_credits, require_api_key
from api.models import (
    ENDPOINT_COSTS,
    BuildWebsiteRequest,
    BuildWebsiteResponse,
    TemplateResponse,
)

router  = APIRouter(prefix="/templates", tags=["Templates & Build"])
logger  = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent.parent.parent


# ─── List all templates ───────────────────────────────────────────────────────
@router.get("/", response_model=list[TemplateResponse],
    summary="List all available website templates",
)
async def list_templates(key_doc: dict = Depends(require_api_key)) -> list[dict]:
    results = []
    for tmpl_file in sorted(BASE_DIR.glob("templates/*/template.json")):
        try:
            data = json.loads(tmpl_file.read_text())
            results.append({
                "template_id":   data.get("template_id", tmpl_file.parent.name),
                "display_name":  data.get("display_name", ""),
                "category":      data.get("category", ""),
                "keywords":      data.get("keywords", []),
                "tech_stack":    data.get("tech_stack", {}),
                "sections":      data.get("sections", []),
                "tested":        data.get("tested", False),
                "estimated_build_minutes": data.get("estimated_build_minutes", 30),
            })
        except Exception as exc:
            logger.warning("Could not load template %s: %s", tmpl_file, exc)
    return results


# ─── Get one template ─────────────────────────────────────────────────────────
@router.get("/{template_id}",
    summary="Get details of a specific template",
)
async def get_template(template_id: str, key_doc: dict = Depends(require_api_key)) -> dict:
    tmpl_file = BASE_DIR / "templates" / template_id / "template.json"
    if not tmpl_file.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return json.loads(tmpl_file.read_text())


# ─── Build website ────────────────────────────────────────────────────────────
@router.post("/build", response_model=BuildWebsiteResponse,
    summary="Build and deploy a full website",
    description=(
        "Full pipeline:\n"
        "1. Match command to template\n"
        "2. Validate prompt via GPT↔Gemini loop\n"
        "3. Cursor → GitHub → MongoDB → Vercel\n"
        "4. Return live URL\n\n"
        "**Cost:** 50 credits per build."
    ),
)
async def build_website(
    req:     BuildWebsiteRequest,
    key_doc: dict = Depends(require_api_key),
) -> dict[str, Any]:
    import sys
    sys.path.insert(0, str(BASE_DIR))

    cost   = ENDPOINT_COSTS["build_website"]
    key_id = str(key_doc["_id"])

    assert_credits(key_doc, cost)

    t0       = time.time()
    live_url = ""
    success  = False
    template_used = "unknown"
    session_id    = ""

    try:
        from main import _match_template, run_website_workflow
        from config.remote_config import fetch_remote_config

        config = {}
        try:
            config = fetch_remote_config()
        except Exception:
            pass

        template = _match_template(req.command)
        if not template:
            raise HTTPException(
                status_code=422,
                detail=f"No template matched for: '{req.command}'. "
                       "Try keywords like: salon, restaurant, portfolio, ecommerce",
            )

        template_used = template.get("template_id", "unknown")
        result_url    = run_website_workflow(
            command=req.command,
            template=template,
            config=config,
            dry_run=False,
        )
        live_url = result_url or ""
        success  = bool(live_url)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Build website failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    duration = time.time() - t0

    from api.keys import deduct_credits
    from api.usage import log_call
    _, remaining = deduct_credits(key_id, cost)
    log_call(
        key_id=key_id,
        endpoint="build_website",
        mode=None,
        credits_used=cost,
        credits_remaining=remaining,
        duration_s=duration,
        success=success,
        metadata={"command": req.command, "template": template_used, "live_url": live_url},
    )

    if not live_url:
        raise HTTPException(status_code=500, detail="Build did not return a live URL")

    return {
        "live_url":          live_url,
        "template_used":     template_used,
        "session_id":        session_id or "N/A",
        "duration_s":        round(duration, 2),
        "credits_used":      cost,
        "credits_remaining": remaining,
    }
