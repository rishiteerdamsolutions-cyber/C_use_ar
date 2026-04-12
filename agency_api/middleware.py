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
async def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key", description="Your API key: ak_live_xxxxx"),
) -> dict[str, Any]:
    """
    FastAPI dependency — validates X-API-Key header.

    Injects the key document into the route handler.
    Raises HTTP 401 if key is missing / invalid / inactive.
    Raises HTTP 429 if rate limit exceeded.

    Usage in route:
        @router.post("/run-workflow")
        async def run_wf(req: ..., key_doc=Depends(require_api_key)):
            credits_remaining = key_doc["credits_total"] - key_doc["credits_used"]
    """
    from agency_api.keys import validate_key
    from agency_api.rate_limiter import check_rate_limit

    # 1. Validate key
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

    # 2. Check credits
    remaining = key_doc["credits_total"] - key_doc["credits_used"]
    if remaining <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error":  "Insufficient credits",
                "code":   402,
                "detail": "Top up your wallet at https://agency.yourplatform.com/billing",
            },
        )

    # 3. Check rate limit
    allowed, reason = check_rate_limit(key_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "Rate limit exceeded", "code": 429, "detail": reason},
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
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "code": 500, "detail": str(exc)},
    )
