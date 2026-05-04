"""
Platform money settings — INR base + manual USD FX rate (Mongo with env fallback).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

_DOC_ID = "money_settings_v1"


def default_fx_inr_per_usd() -> float:
    raw = (os.environ.get("PLATFORM_FX_INR_PER_USD") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 83.0


def get_money_settings() -> dict[str, Any]:
    """Return canonical money settings (INR base + fx for USD display)."""
    base = "INR"
    fx = default_fx_inr_per_usd()
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    try:
        from agency_api.database import get_collection, Collections

        col = get_collection(Collections.PLATFORM_SETTINGS)
        doc = col.find_one({"_id": _DOC_ID})
        if doc and isinstance(doc.get("fx_inr_per_usd"), (int, float)) and float(doc["fx_inr_per_usd"]) > 0:
            fx = float(doc["fx_inr_per_usd"])
            updated_at = doc.get("updated_at")
            updated_by = doc.get("updated_by")
    except Exception:
        pass
    return {
        "base_currency": base,
        "fx_inr_per_usd": fx,
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


def set_money_settings(*, fx_inr_per_usd: float, updated_by: str) -> dict[str, Any]:
    if fx_inr_per_usd <= 0:
        raise ValueError("fx_inr_per_usd must be positive")
    from agency_api.database import get_collection, Collections

    now = datetime.now(timezone.utc).isoformat()
    col = get_collection(Collections.PLATFORM_SETTINGS)
    col.update_one(
        {"_id": _DOC_ID},
        {
            "$set": {
                "fx_inr_per_usd": float(fx_inr_per_usd),
                "updated_at": now,
                "updated_by": (updated_by or "")[:200],
            }
        },
        upsert=True,
    )
    return get_money_settings()
