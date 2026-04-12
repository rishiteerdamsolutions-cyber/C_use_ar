"""
FastAPI Server — Autonomous Web Agency Platform v1.0
Entry point for the public REST API.

Run locally:
    uvicorn agency_api.server:app --reload --port 8000

Run via Docker:
    docker-compose up
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agency_api.middleware import global_exception_handler, logging_middleware
from agency_api.routes import billing, keys, templates, trainer, validator, workflows
from whitelabel.api_routes import router as whitelabel_router
from whitelabel.admin_panel.routes import router as admin_router
from whitelabel.router import TenantMiddleware

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
)
logger = logging.getLogger("agency_api.server")

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
        from agency_api.database import get_db
        get_db()
        logger.info("  ✓ MongoDB connected")
    except Exception as exc:
        logger.warning("  ✗ MongoDB unavailable: %s", exc)

    # Warm Redis
    try:
        from agency_api.rate_limiter import _get_redis
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
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:7788,http://localhost:8000,"
        "https://cuseai.vercel.app,https://yourplatform.com",
    ).split(",")
    if o.strip()
]

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
app.include_router(trainer.router)                       # /api/trainer/... (Mongo, no live run)

# ─── Marketing site (portal/*.html) — local + Vercel ───────────────────────────
_PORTAL_DIR = Path(__file__).resolve().parent.parent / "portal"
if _PORTAL_DIR.is_dir():
    app.mount(
        "/site",
        StaticFiles(directory=str(_PORTAL_DIR), html=True),
        name="portal_site",
    )


# ─── Root endpoints ────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    """
    Serve the marketing homepage as HTML (Vercel sends all traffic here via rewrite).
    Redirect-only broke when `portal/` was missing from the serverless bundle; inline
    FileResponse works when `includeFiles: portal/**` is set in vercel.json.
    """
    index = _PORTAL_DIR / "index.html"
    if index.is_file():
        return FileResponse(str(index), media_type="text/html; charset=utf-8")
    if _PORTAL_DIR.is_dir():
        return RedirectResponse(url="/site/", status_code=302)
    return JSONResponse({
        "service":  "Autonomous Web Agency API",
        "version":  VERSION,
        "status":   "ok",
        "website":  "/site/",
        "docs":     "/docs",
        "base_url": "/api/v1",
    })


@app.get("/api-meta", include_in_schema=False)
async def api_meta():
    return JSONResponse({
        "service":  "Autonomous Web Agency API",
        "version":  VERSION,
        "status":   "ok",
        "website":  "/site/",
        "trainer_api": "/api/trainer",
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


def _register_portal_html_routes() -> None:
    """Top-level /trainer.html etc. for Vercel (single rewrite to /api/index)."""
    if not _PORTAL_DIR.is_dir():
        return
    for name in ("trainer.html", "pricing.html", "signup.html", "dashboard.html", "docs.html"):
        fp = _PORTAL_DIR / name
        if not fp.is_file():
            continue

        def _make(n: str, path: Path):
            async def _send() -> FileResponse:
                if path.is_file():
                    return FileResponse(str(path), media_type="text/html; charset=utf-8")
                raise HTTPException(status_code=404)

            return _send

        app.add_api_route(
            f"/{name}",
            _make(name, fp),
            methods=["GET"],
            name=f"portal_{name.replace('.', '_')}",
            include_in_schema=False,
        )


_register_portal_html_routes()


# ─── Run directly ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn  # type: ignore
    uvicorn.run(
        "agency_api.server:app",
        host    = "0.0.0.0",
        port    = int(os.environ.get("PORT", "8000")),
        reload  = os.environ.get("ENV", "production") == "development",
        workers = int(os.environ.get("WORKERS", "1")),
    )
