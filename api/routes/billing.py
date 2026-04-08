"""
Routes — /api/v1/billing/*
Razorpay order creation and webhook.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.middleware import require_api_key
from api.models import CREDIT_PACKS, CreateOrderRequest, CreateOrderResponse

router = APIRouter(prefix="/billing", tags=["Billing"])
logger = logging.getLogger(__name__)


@router.post("/create-order", response_model=CreateOrderResponse,
    summary="Create a Razorpay order to top up credits",
    description=(
        "Returns a Razorpay order_id. Use this with Razorpay Checkout in your frontend "
        "to complete payment. After payment, the webhook auto-credits your wallet."
    ),
)
async def create_order(
    req:     CreateOrderRequest,
    key_doc: dict = Depends(require_api_key),
) -> dict[str, Any]:
    from api.billing import create_order as _create

    pack_info = CREDIT_PACKS.get(req.pack)
    if not pack_info:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pack '{req.pack}'. Valid: {list(CREDIT_PACKS.keys())}",
        )

    key_id = str(key_doc["_id"])

    try:
        order = _create(
            amount_inr=pack_info["inr"],
            pack=req.pack,
            key_id=key_id,
        )
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Razorpay not configured on this server. Contact support.",
        )
    except Exception as exc:
        logger.error("Order creation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "order_id":     order["order_id"],
        "amount_paise": order["amount_paise"],
        "currency":     "INR",
        "pack":         req.pack,
        "credits":      pack_info["credits"],
        "key_id":       order.get("key_id_public", ""),
    }


@router.post("/webhook",
    summary="Razorpay webhook receiver (internal)",
    description="Called by Razorpay after payment. Verifies signature and credits wallet.",
    include_in_schema=False,   # hide from public docs
)
async def razorpay_webhook(request: Request) -> dict[str, Any]:
    from api.billing import handle_webhook

    raw_body  = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    result = handle_webhook(raw_body, signature)

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "webhook_failed"))

    return result


@router.get("/packs",
    summary="List credit packs and prices",
)
async def list_packs() -> dict[str, Any]:
    return {
        "currency": "INR",
        "packs": [
            {
                "id":       k,
                "label":    v["label"],
                "price":    v["inr"],
                "credits":  v["credits"],
                "per_credit_inr": round(v["inr"] / v["credits"], 2),
            }
            for k, v in CREDIT_PACKS.items()
        ],
    }
