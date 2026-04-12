"""
Pydantic Models — request bodies and response shapes.
Autonomous Web Agency Platform · API Layer
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, EmailStr


# ─── Enums ────────────────────────────────────────────────────────────────────
class RunMode(str, Enum):
    fast  = "fast"   # V1 Training-Only
    smart = "smart"  # V2 Claude Vision


class KeyStatus(str, Enum):
    active    = "active"
    suspended = "suspended"
    expired   = "expired"


class BillingEvent(str, Enum):
    topup    = "topup"
    debit    = "debit"
    refund   = "refund"


# ─── Credit pack options ──────────────────────────────────────────────────────
CREDIT_PACKS = {
    "starter":      {"inr": 999,  "credits": 500,   "label": "Starter"},
    "professional": {"inr": 2999, "credits": 2000,  "label": "Professional"},
    "agency":       {"inr": 9999, "credits": 10000, "label": "Agency"},
}

# Cost per endpoint in credits
ENDPOINT_COSTS = {
    "run_workflow_smart": 5,
    "run_workflow_fast":  2,
    "teach":              3,   # per screenshot
    "validate_prompt":    2,
    "build_website":      50,
}


# ─── API Key models ───────────────────────────────────────────────────────────
class GenerateKeyRequest(BaseModel):
    owner_name:  str        = Field(..., min_length=2, max_length=100)
    owner_email: EmailStr
    pack:        str        = Field("starter", description="starter | professional | agency")
    razorpay_payment_id: Optional[str] = None


class GenerateKeyResponse(BaseModel):
    api_key:    str   # shown ONCE — ak_live_xxxxx
    key_id:     str   # internal ID (hashed key reference)
    credits:    int
    owner_name: str
    created_at: datetime


class KeyUsageResponse(BaseModel):
    key_id:          str
    owner_name:      str
    credits_total:   int
    credits_used:    int
    credits_remaining: int
    calls_today:     int
    calls_this_month: int
    status:          KeyStatus
    created_at:      datetime
    last_used_at:    Optional[datetime]


# ─── Workflow models ──────────────────────────────────────────────────────────
class StepDefinition(BaseModel):
    screenshot:  str   # filename
    instruction: str   # plain-English description


class TeachRequest(BaseModel):
    workflow_name: str         = Field(..., min_length=2, max_length=80)
    steps:         list[StepDefinition]


class TeachResponse(BaseModel):
    workflow_name: str
    total_steps:   int
    workflow_json: dict[str, Any]
    credits_used:  int
    credits_remaining: int


class RunWorkflowRequest(BaseModel):
    workflow_name: str
    mode:          RunMode  = RunMode.smart
    variables:     dict[str, str] = {}


class RunWorkflowResponse(BaseModel):
    workflow_name: str
    mode:          RunMode
    success:       bool
    session_id:    str
    live_url:      Optional[str] = None
    steps_run:     int
    success_rate:  float
    duration_s:    float
    credits_used:  int
    credits_remaining: int


# ─── Build website models ─────────────────────────────────────────────────────
class BuildWebsiteRequest(BaseModel):
    command:       str   = Field(..., description="e.g. 'Build salon website for Priya'")
    client_name:   Optional[str] = None
    client_phone:  Optional[str] = None
    notify_via:    Optional[str] = Field("whatsapp", description="whatsapp | email | none")


class BuildWebsiteResponse(BaseModel):
    live_url:       str
    template_used:  str
    session_id:     str
    duration_s:     float
    credits_used:   int
    credits_remaining: int


# ─── Prompt validation models ────────────────────────────────────────────────
class ValidatePromptRequest(BaseModel):
    prompt:         str = Field(..., min_length=10)
    max_iterations: int = Field(3, ge=1, le=5)


class ValidatePromptResponse(BaseModel):
    validated_prompt: str
    iterations:       int
    status:           str
    credits_used:     int
    credits_remaining: int


# ─── Template models ──────────────────────────────────────────────────────────
class TemplateResponse(BaseModel):
    template_id:    str
    display_name:   str
    category:       str
    keywords:       list[str]
    tech_stack:     dict[str, str]
    sections:       list[dict]
    tested:         bool
    estimated_build_minutes: int


# ─── Billing models ───────────────────────────────────────────────────────────
class CreateOrderRequest(BaseModel):
    pack:        str       = Field(..., description="starter | professional | agency")
    owner_email: EmailStr


class CreateOrderResponse(BaseModel):
    order_id:    str        # Razorpay order_id
    amount_paise: int       # amount in paise (INR × 100)
    currency:    str        = "INR"
    pack:        str
    credits:     int
    key_id:      str        # Razorpay key_id (public) for frontend


# ─── Generic responses ────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status:  str = "ok"
    version: str
    uptime_s: float


class ErrorResponse(BaseModel):
    error:   str
    detail:  Optional[str] = None
    code:    int
