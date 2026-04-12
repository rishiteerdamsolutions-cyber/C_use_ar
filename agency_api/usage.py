"""
Usage Tracker — log every API call, enforce credit deduction.
Autonomous Web Agency Platform · API Layer
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def log_call(
    key_id:       str,
    endpoint:     str,
    mode:         Optional[str],
    credits_used: int,
    credits_remaining: int,
    duration_s:   float,
    success:      bool,
    metadata:     Optional[dict[str, Any]] = None,
) -> str:
    """
    Persist one API call record to MongoDB usage_logs.

    Args:
        key_id:            MongoDB _id of the API key used.
        endpoint:          e.g. 'run_workflow', 'teach', 'build_website'
        mode:              'fast' | 'smart' | None
        credits_used:      Credits deducted for this call.
        credits_remaining: Balance after deduction.
        duration_s:        Wall-clock seconds the call took.
        success:           Whether the call completed successfully.
        metadata:          Optional extra info (workflow_name, live_url, etc.)

    Returns:
        log_id (MongoDB inserted _id as string)
    """
    from agency_api.database import get_collection, Collections

    doc = {
        "key_id":             key_id,
        "endpoint":           endpoint,
        "mode":               mode,
        "credits_used":       credits_used,
        "credits_remaining":  credits_remaining,
        "duration_s":         round(duration_s, 3),
        "success":            success,
        "timestamp":          datetime.now(timezone.utc),
        "metadata":           metadata or {},
    }

    col    = get_collection(Collections.USAGE)
    result = col.insert_one(doc)
    log_id = str(result.inserted_id)

    logger.info(
        "API call logged  endpoint=%s  mode=%s  credits=%d  success=%s  duration=%.2fs",
        endpoint, mode, credits_used, success, duration_s,
    )
    return log_id


def get_call_history(key_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """
    Return the most recent API calls for a given key.

    Args:
        key_id: MongoDB _id of the API key.
        limit:  Max records to return (default 50).

    Returns:
        List of call log dicts, newest first.
    """
    from agency_api.database import get_collection, Collections

    col  = get_collection(Collections.USAGE)
    docs = col.find(
        {"key_id": key_id},
        sort=[("timestamp", -1)],
        limit=limit,
    )
    return [
        {**d, "_id": str(d["_id"]), "timestamp": d["timestamp"].isoformat()}
        for d in docs
    ]


def get_platform_stats() -> dict[str, Any]:
    """
    Aggregate platform-wide stats (admin use).

    Returns:
        Dict with total_calls, total_credits_used, active_keys, etc.
    """
    from agency_api.database import get_collection, Collections

    usage_col = get_collection(Collections.USAGE)
    keys_col  = get_collection(Collections.API_KEYS)

    total_calls   = usage_col.count_documents({})
    success_calls = usage_col.count_documents({"success": True})
    active_keys   = keys_col.count_documents({"status": "active"})

    pipeline = [{"$group": {"_id": None, "total": {"$sum": "$credits_used"}}}]
    agg = list(usage_col.aggregate(pipeline))
    total_credits = agg[0]["total"] if agg else 0

    return {
        "total_calls":         total_calls,
        "successful_calls":    success_calls,
        "success_rate":        round(success_calls / max(total_calls, 1), 4),
        "total_credits_used":  total_credits,
        "active_keys":         active_keys,
    }
