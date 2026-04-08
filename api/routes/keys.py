"""
Routes — /api/v1/keys/*
API key generation and usage stats.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from api.middleware import require_api_key
from api.models import (
    CREDIT_PACKS,
    GenerateKeyRequest,
    GenerateKeyResponse,
    KeyUsageResponse,
)

router = APIRouter(prefix="/keys", tags=["API Keys"])
logger = logging.getLogger(__name__)


@router.post("/generate", response_model=GenerateKeyResponse, status_code=201,
    summary="Generate a new API key",
    description=(
        "Creates a new API key and returns it **once** — store it securely. "
        "Requires a valid Razorpay payment_id for paid packs, or use pack='starter' for free trial."
    ),
)
async def generate_key(req: GenerateKeyRequest) -> dict[str, Any]:
    from api.keys import create_key
    from api.models import CREDIT_PACKS

    pack_info = CREDIT_PACKS.get(req.pack)
    if not pack_info:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown pack '{req.pack}'. Choose: {list(CREDIT_PACKS.keys())}",
        )

    # For paid packs, a Razorpay payment_id must be present
    if req.pack != "starter" and not req.razorpay_payment_id:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Paid packs require a razorpay_payment_id. "
                   "Use POST /billing/create-order to initiate payment.",
        )

    raw_key, key_id = create_key(
        owner_name=req.owner_name,
        owner_email=str(req.owner_email),
        credits=pack_info["credits"],
        pack=req.pack,
        razorpay_payment_id=req.razorpay_payment_id,
    )

    from datetime import datetime, timezone
    return {
        "api_key":    raw_key,
        "key_id":     key_id,
        "credits":    pack_info["credits"],
        "owner_name": req.owner_name,
        "created_at": datetime.now(timezone.utc),
    }


@router.get("/usage", response_model=KeyUsageResponse,
    summary="Get usage stats for your API key",
)
async def get_usage(key_doc: dict = Depends(require_api_key)) -> dict[str, Any]:
    from api.keys import get_usage_summary
    return get_usage_summary(str(key_doc["_id"]))


@router.get("/packs",
    summary="List available credit packs and pricing",
)
async def list_packs() -> dict[str, Any]:
    return {
        "packs": [
            {
                "id":      pack_id,
                "label":   info["label"],
                "inr":     info["inr"],
                "credits": info["credits"],
                "cost_per_credit": round(info["inr"] / info["credits"], 2),
            }
            for pack_id, info in CREDIT_PACKS.items()
        ],
        "endpoint_costs": {
            "run_workflow_smart": "5 credits",
            "run_workflow_fast":  "2 credits",
            "teach_per_screenshot": "3 credits",
            "validate_prompt":    "2 credits",
            "build_website":      "50 credits",
        },
    }
