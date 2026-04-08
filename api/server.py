"""
FastAPI Server — Autonomous Web Agency Platform v1.0
Entry point for the public REST API.

Run locally:
    uvicorn api.server:app --reload --port 8000

Run via Docker:
    docker-compose up
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.middleware import global_exception_handler, logging_middleware
from api.routes import billing, keys, templates, validator, workflows
from whitelabel.api_routes import router as whitelabel_router
from whitelabel.admin_panel.routes import router as admin_router
from whitelabel.router import TenantMiddleware

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
)
logger = logging.getLogger("api.server")

_START_TIME = time.time()
VERSION     = (Path(__file__).parent.parent / "VERSION").read_text().strip()


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────────────
    logger.info("━" * 55)
    logger.info("  Autonomous Web Agency API  v%s  starting", VERSION)
    logger.info("━" * 55)

    # Verify MongoDB
    try:
        from api.database import get_db
        get_db()
        logger.info("  ✓ MongoDB connected")
    except Exception as exc:
        logger.warning("  ✗ MongoDB unavailable: %s", exc)

    # Warm Redis
    try:
        from api.rate_limiter import _get_redis
        r = _get_redis()
        if r:
            logger.info("  ✓ Redis connected")
    except Exception:
        logger.info("  ⚠ Redis not available — using in-memory rate limiter")

    logger.info("  Docs: http://localhost:8000/docs")
    logger.info("━" * 55)

    yield   # server runs here

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("API server shutting down")


# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "Autonomous Web Agency API",
    description = (
        "Build and deploy full websites via API. "
        "Teach workflows from screenshots, run them in Fast or AI mode, "
        "validate prompts with GPT↔Gemini loop.\n\n"
        "**Authentication:** Pass your API key in every request header:\n"
        "`X-API-Key: ak_live_your_key_here`\n\n"
        "**Credits:** Each endpoint costs a fixed number of credits. "
        "Top up via POST /billing/create-order (Razorpay).\n\n"
        "**Base URL:** `https://api.yourplatform.com/api/v1`"
    ),
    version     = VERSION,
    lifespan    = lifespan,
    docs_url    = "/docs",
    redoc_url   = "/redoc",
    contact     = {
        "name":  "Autonomous Web Agency",
        "email": "support@yourplatform.com",
        "url":   "https://yourplatform.com",
    },
    license_info= {"name": "Proprietary"},
)

# ─── CORS ─────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:7788,https://yourplatform.com",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ─── Custom middleware ─────────────────────────────────────────────────────────
app.middleware("http")(logging_middleware)
app.add_exception_handler(Exception, global_exception_handler)
app.add_middleware(TenantMiddleware)

# ─── Routers ─────────────────────────────────────────────────────────────────
PREFIX = "/api/v1"
app.include_router(keys.router,         prefix=PREFIX)
app.include_router(workflows.router,    prefix=PREFIX)
app.include_router(templates.router,    prefix=PREFIX)
app.include_router(validator.router,    prefix=PREFIX)
app.include_router(billing.router,      prefix=PREFIX)
app.include_router(whitelabel_router,   prefix=PREFIX)   # /api/v1/whitelabel/...
app.include_router(admin_router)                         # /admin/...  (tenant-aware)


# ─── Root endpoints ────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({
        "service":  "Autonomous Web Agency API",
        "version":  VERSION,
        "status":   "ok",
        "docs":     "/docs",
        "base_url": "/api/v1",
    })


@app.get("/api/v1/health", tags=["System"],
    summary="Health check — returns server status and uptime",
)
async def health():
    return {
        "status":   "ok",
        "version":  VERSION,
        "uptime_s": round(time.time() - _START_TIME, 1),
    }


# ─── Run directly ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn  # type: ignore
    uvicorn.run(
        "api.server:app",
        host    = "0.0.0.0",
        port    = int(os.environ.get("PORT", "8000")),
        reload  = os.environ.get("ENV", "production") == "development",
        workers = int(os.environ.get("WORKERS", "1")),
    )
