"""
Local AI run counter for trainer/desktop when Mongo is not used.
File: <AGENCY_HOME>/.cusear_entitlements_usage.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _state_path() -> Path:
    root = (os.environ.get("AGENCY_HOME") or ".").strip() or "."
    return Path(root) / ".cusear_entitlements_usage.json"


def _read() -> dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_local_ai_runs_this_month() -> int:
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    st = _read()
    if st.get("month") != month:
        return 0
    try:
        return int(st.get("ai_runs", 0))
    except (TypeError, ValueError):
        return 0


def increment_local_ai_run() -> int:
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    st = _read()
    if st.get("month") != month:
        st = {"month": month, "ai_runs": 0}
    st["month"] = month
    st["ai_runs"] = int(st.get("ai_runs") or 0) + 1
    _write(st)
    return int(st["ai_runs"])
