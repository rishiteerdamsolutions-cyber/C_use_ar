"""
Routes — /api/v1/billing/*
Razorpay order creation and webhook.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from agency_api.models import (
    CREDIT_PACKS,
    CreateOrderRequest,
    CreateOrderResponse,
    MoneySettingsResponse,
    PlanMarginValidateRequest,
    UpdateMoneySettingsRequest,
)

router = APIRouter(prefix="/billing", tags=["Billing"])
logger = logging.getLogger(__name__)


def _require_platform_admin(x_admin_token: str | None) -> None:
    expected = (os.environ.get("PLATFORM_ADMIN_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="PLATFORM_ADMIN_TOKEN not configured")
    if (x_admin_token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@router.post("/create-order", response_model=CreateOrderResponse,
    summary="Create a Razorpay order to top up credits",
    description=(
        "Returns a Razorpay order_id. Use this with Razorpay Checkout in your frontend "
        "to complete payment. After payment, the webhook auto-credits your wallet."
    ),
)
async def create_order(
    req:     CreateOrderRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    from agency_api.billing import create_order as _create

    pack_info = CREDIT_PACKS.get(req.pack)
    if not pack_info:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pack '{req.pack}'. Valid: {list(CREDIT_PACKS.keys())}",
        )

    key_id = "public_checkout"
    if x_api_key:
        from agency_api.keys import validate_key
        from agency_api.rate_limiter import check_rate_limit

        key_doc = validate_key(x_api_key)
        if not key_doc:
            raise HTTPException(status_code=401, detail="Invalid or inactive API key")
        allowed, reason = check_rate_limit(str(key_doc["_id"]))
        if not allowed:
            raise HTTPException(status_code=429, detail=reason)
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
        logger.error("Order creation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Order creation failed")

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
    from agency_api.billing import handle_webhook

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


@router.get(
    "/money-settings",
    response_model=MoneySettingsResponse,
    summary="INR base + USD FX (manual rate)",
)
async def get_money_settings() -> dict[str, Any]:
    from agency_api.money_settings import get_money_settings as _gs

    return _gs()


@router.post(
    "/money-settings",
    response_model=MoneySettingsResponse,
    summary="Update USD FX rate (platform admin)",
)
async def post_money_settings(
    req: UpdateMoneySettingsRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_platform_admin(x_admin_token)
    from agency_api.money_settings import set_money_settings as _ss

    return _ss(fx_inr_per_usd=req.fx_inr_per_usd, updated_by="platform_admin")


@router.post(
    "/validate-plan-margin",
    summary="Validate projected gross margin vs variable costs (admin)",
)
async def post_validate_plan_margin(
    req: PlanMarginValidateRequest,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_platform_admin(x_admin_token)
    from agency_api.pricing_policy import validate_plan_margin as _vm

    return _vm(
        plan_price_inr=req.plan_price_inr,
        expected_ai_runs_per_month=req.expected_ai_runs_per_month,
    )
