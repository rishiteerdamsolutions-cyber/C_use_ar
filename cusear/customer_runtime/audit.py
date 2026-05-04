"""Run audit logging for customer app workflow executions."""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from .paths import run_audit_dir


def _slug(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    return clean or "workflow"


def save_run_audit(
    *,
    workflow_name: str,
    dry_run: bool,
    steps: list[dict[str, Any]],
    error: str = "",
    bundle_slug: str = "",
) -> Path:
    now = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    payload = {
        "workflow_name": workflow_name,
        "bundle_slug": bundle_slug,
        "dry_run": bool(dry_run),
        "started_at": steps[0].get("started_at") if steps else now,
        "finished_at": now,
        "status": "error" if error else "ok",
        "error": error,
        "steps": steps,
    }
    filename = f"{now.replace(':', '').replace('-', '')}_{_slug(bundle_slug or workflow_name)}.json"
    path = run_audit_dir() / filename
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
