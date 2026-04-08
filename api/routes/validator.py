"""
Routes — /api/v1/validate-prompt
GPT-4o ↔ Gemini prompt validation loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends

from api.middleware import assert_credits, require_api_key
from api.models import ENDPOINT_COSTS, ValidatePromptRequest, ValidatePromptResponse

router = APIRouter(prefix="/validate-prompt", tags=["Prompt Validator"])
logger = logging.getLogger(__name__)


@router.post("/", response_model=ValidatePromptResponse,
    summary="Validate and refine a website build prompt",
    description=(
        "Runs the GPT-4o ↔ Gemini validation loop (max 3 iterations by default).\n\n"
        "GPT-4o refines the prompt → Gemini validates → if approved, returns it.\n"
        "If Gemini says NEEDS_IMPROVEMENT, GPT refines again. Stops at max_iterations.\n\n"
        "**Cost:** 2 credits per call."
    ),
)
async def validate_prompt(
    req:     ValidatePromptRequest,
    key_doc: dict = Depends(require_api_key),
) -> dict[str, Any]:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    cost   = ENDPOINT_COSTS["validate_prompt"]
    key_id = str(key_doc["_id"])

    assert_credits(key_doc, cost)

    t0      = time.time()
    success = False
    result: dict[str, Any] = {}

    try:
        from ai.validator import validate_prompt as _validate
        result  = _validate(req.prompt, max_iterations=req.max_iterations)
        success = True
    except Exception as exc:
        logger.error("Prompt validation error: %s", exc)
        raise

    duration = time.time() - t0

    from api.keys import deduct_credits
    from api.usage import log_call
    _, remaining = deduct_credits(key_id, cost)
    log_call(
        key_id=key_id,
        endpoint="validate_prompt",
        mode=None,
        credits_used=cost,
        credits_remaining=remaining,
        duration_s=duration,
        success=success,
        metadata={"iterations": result.get("iterations"), "status": result.get("status")},
    )

    return {
        "validated_prompt":  result.get("validated_prompt", req.prompt),
        "iterations":        result.get("iterations", 0),
        "status":            result.get("status", "unknown"),
        "credits_used":      cost,
        "credits_remaining": remaining,
    }
