"""
Whitelabel Public API Routes — onboarding & order creation.
Autonomous Web Agency Platform · White-label Layer

Mounted at /whitelabel on the main API server.
These endpoints are called by:
  - The public signup form on the developer portal
  - Razorpay webhook (after payment confirmed)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, field_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whitelabel", tags=["White-label Onboarding"])


# ─── Request / Response models ────────────────────────────────────────────────

class OnboardRequest(BaseModel):
    agency_name:  str
    subdomain:    str
    owner_name:   str
    owner_email:  EmailStr
    owner_phone:  str
    payment_id:   str           # Razorpay payment_id (verified by webhook first)
    primary_color: Optional[str] = "#c9a96e"
    logo_url:      Optional[str] = None

    @field_validator("subdomain")
    @classmethod
    def subdomain_clean(cls, v: str) -> str:
        import re
        slug = re.sub(r"[^a-z0-9-]", "-", v.lower().strip())
        slug = re.sub(r"-+", "-", slug).strip("-")
        if len(slug) < 3:
            raise ValueError("Subdomain must be at least 3 characters")
        if len(slug) > 30:
            raise ValueError("Subdomain must be 30 characters or less")
        return slug

    @field_validator("agency_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Agency name cannot be empty")
        return v


class OrderRequest(BaseModel):
    agency_name:  str
    subdomain:    str
    owner_email:  EmailStr


class OnboardResponse(BaseModel):
    tenant_id:        str
    subdomain:        str
    portal_url:       str
    admin_api_key:    str   # shown ONCE — instruct client to save it
    credits_included: int
    message:          str


class OrderResponse(BaseModel):
    order_id:   str
    amount:     int
    currency:   str
    key_id:     str         # Razorpay key_id for the frontend


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post(
    "/create-order",
    response_model=OrderResponse,
    summary="Create Razorpay order for ₹9,999/mo white-label plan",
)
async def create_whitelabel_order(body: OrderRequest) -> dict[str, Any]:
    """
    Step 1 of signup: create a Razorpay order.
    The frontend opens the Razorpay checkout modal with the returned order_id.
    On successful payment, Razorpay fires the webhook → /api/v1/billing/webhook.
    Then call /whitelabel/onboard with the resulting payment_id.
    """
    import os
    from whitelabel.onboarding import create_whitelabel_order as _create_order

    try:
        order = _create_order(
            agency_name=body.agency_name,
            subdomain=body.subdomain,
            owner_email=body.owner_email,
        )
    except Exception as exc:
        logger.error("Order creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Payment gateway error: {exc}",
        )

    return {
        "order_id":  order.get("id", ""),
        "amount":    order.get("amount", 999900),
        "currency":  order.get("currency", "INR"),
        "key_id":    os.environ.get("RAZORPAY_KEY_ID", ""),
    }


@router.post(
    "/onboard",
    response_model=OnboardResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Complete agency onboarding after successful payment",
)
async def onboard_agency(body: OnboardRequest) -> dict[str, Any]:
    """
    Step 2 of signup: called after Razorpay payment succeeds.
    Creates the tenant, issues API key, sends welcome email.

    The payment_id is stored for audit — production should also verify
    it against the Razorpay webhook record before trusting it.
    """
    from whitelabel.onboarding import onboard_agency as _onboard

    try:
        result = _onboard(
            agency_name=body.agency_name,
            subdomain=body.subdomain,
            owner_name=body.owner_name,
            owner_email=body.owner_email,
            owner_phone=body.owner_phone,
            branding={
                "primary_color": body.primary_color or "#c9a96e",
                **({"logo_url": body.logo_url} if body.logo_url else {}),
            },
            payment_id=body.payment_id,
        )
    except ValueError as exc:
        # e.g. subdomain already taken
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error("Onboarding failed for %s: %s", body.owner_email, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Onboarding failed — please contact support",
        )

    return result


@router.get(
    "/check-subdomain/{subdomain}",
    summary="Check if a subdomain is available",
)
async def check_subdomain(subdomain: str) -> dict[str, Any]:
    """Quick availability check — call from signup form on blur."""
    import re
    slug = re.sub(r"[^a-z0-9-]", "-", subdomain.lower().strip())

    from whitelabel.tenant_config import get_tenant_by_subdomain
    taken = get_tenant_by_subdomain(slug) is not None
    return {
        "subdomain": slug,
        "available": not taken,
        "portal_url": f"https://{slug}.yourplatform.com",
    }


@router.get(
    "/plans",
    summary="List available white-label plans and pricing",
)
async def list_plans() -> dict[str, Any]:
    return {
        "plans": [
            {
                "id":           "whitelabel_monthly",
                "name":         "Agency White-label",
                "price_inr":    9999,
                "billing":      "monthly",
                "credits":      10_000,
                "features": [
                    "Your own branded subdomain",
                    "Custom domain (CNAME)",
                    "10,000 AI credits/month",
                    "Full API access",
                    "Admin dashboard",
                    "Branding customisation",
                    "Priority support",
                ],
            }
        ]
    }
