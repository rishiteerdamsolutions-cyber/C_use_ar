"""
Database — MongoDB Atlas singleton connection.
Autonomous Web Agency Platform · API Layer
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_client = None
_db     = None


def get_db():
    """
    Return the MongoDB database instance (lazy singleton).
    Reads MONGODB_URI from environment.
    """
    global _client, _db
    if _db is not None:
        return _db

    try:
        from pymongo import MongoClient          # type: ignore
        from pymongo.server_api import ServerApi # type: ignore

        uri = os.environ.get("MONGODB_URI", "")
        if not uri:
            raise ValueError("MONGODB_URI environment variable not set")

        _client = MongoClient(uri, server_api=ServerApi("1"), serverSelectionTimeoutMS=5000)
        _client.admin.command("ping")           # verify connection
        _db = _client[os.environ.get("MONGODB_DB_NAME", "agency_platform")]
        logger.info("MongoDB connected — db=%s", _db.name)

    except Exception as exc:
        logger.error("MongoDB connection failed: %s", exc)
        raise

    return _db


def get_collection(name: str):
    """Shortcut: get a named collection from the default DB."""
    return get_db()[name]


# ── Collection names (centralised to avoid typos) ─────────────────────────────
class Collections:
    API_KEYS  = "api_keys"       # hashed keys, owner info, credit balance
    USAGE     = "usage_logs"     # every API call logged here
    BILLING   = "billing"        # Razorpay orders and webhook events
    TENANTS   = "tenants"        # white-label agency configs
    TEMPLATES = "templates"      # website template metadata
