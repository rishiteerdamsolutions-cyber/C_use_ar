"""
Middleware — API key auth, rate limiting, structured request logging.
Autonomous Web Agency Platform · API Layer
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


# ─── Auth dependency ─────────────────────────────────────────────────────────
def _validate_key_and_rate_limit(x_api_key: str) -> dict[str, Any]:
    """Shared validator: key status + rate limit (no credit checks)."""
    from agency_api.keys import validate_key
    from agency_api.rate_limiter import check_rate_limit

    key_doc = validate_key(x_api_key)
    if not key_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error":  "Invalid or inactive API key",
                "code":   401,
                "detail": "Pass your key in the X-API-Key header: ak_live_xxxxx",
            },
        )

    key_id = str(key_doc["_id"])

    allowed, reason = check_rate_limit(key_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "Rate limit exceeded", "code": 429, "detail": reason},
        )

    return key_doc


async def require_api_key_allow_zero_credits(
    x_api_key: str = Header(..., alias="X-API-Key", description="Your API key: ak_live_xxxxx"),
) -> dict[str, Any]:
    """
    Validate key + rate-limit checks, but allow 0-credit keys.

    Used for top-up/payment endpoints where users must be able to recharge.
    """
    return _validate_key_and_rate_limit(x_api_key)


async def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key", description="Your API key: ak_live_xxxxx"),
) -> dict[str, Any]:
    """
    Validate key + rate limit + positive credit balance.
    """
    key_doc = _validate_key_and_rate_limit(x_api_key)
    remaining = key_doc["credits_total"] - key_doc["credits_used"]
    if remaining <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error":  "Insufficient credits",
                "code":   402,
                "detail": "Top up your wallet from the dashboard billing page.",
            },
        )
    return key_doc


# ─── Credits check helper ─────────────────────────────────────────────────────
def assert_credits(key_doc: dict[str, Any], needed: int) -> None:
    """
    Raise HTTP 402 if the key doesn't have enough credits for the operation.

    Call this inside route handlers BEFORE doing expensive work.
    """
    remaining = key_doc["credits_total"] - key_doc["credits_used"]
    if remaining < needed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error":  "Insufficient credits",
                "code":   402,
                "detail": f"This operation costs {needed} credits. You have {remaining}.",
            },
        )


# ─── Request logging middleware ───────────────────────────────────────────────
async def logging_middleware(request: Request, call_next):
    """
    Log every request: method, path, status, duration.
    Attach as app.middleware("http").
    """
    start = time.time()
    # Don't log the API key itself
    response = await call_next(request)
    duration = time.time() - start

    logger.info(
        "%s %s → %d  (%.3fs)  ip=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration,
        request.client.host if request.client else "unknown",
    )

    # Attach timing header
    response.headers["X-Response-Time"] = f"{duration:.3f}s"
    return response


# ─── Global exception handler ─────────────────────────────────────────────────
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all — return clean JSON instead of HTML 500 pages."""
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    import os
    is_dev = os.environ.get("ENV", "").strip().lower() == "development"
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "code": 500,
            "detail": str(exc) if is_dev else "Unexpected server error",
        },
    )
