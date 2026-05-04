"""
Vercel serverless entry — ASGI via Mangum.

Local dev: use `uvicorn agency_api.server:app --reload` (not this file).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mangum import Mangum
from agency_api.server import app as _fastapi_app

# Vercel Python expects a top-level ASGI ``app`` in ``api/*.py`` (see Vercel Python runtime docs).
# "off" = skip FastAPI lifespan on cold start (Mongo/Redis are optional for many routes).
app = Mangum(_fastapi_app, lifespan="off")
