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
from agency_api.server import app

# "off" = skip FastAPI lifespan on cold start (Mongo/Redis are optional for many routes)
handler = Mangum(app, lifespan="off")
