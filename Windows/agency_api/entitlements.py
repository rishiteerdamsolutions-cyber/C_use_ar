"""
Entitlements — module access, AI vs non-AI workflow policy, local + API guards.
cusear™ Platform

Feature flag: ENTITLEMENTS_ENFORCEMENT=1 enables checks. Default off for backward compatibility.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# Product module ids (internal; UI uses ar™)
MODULE_IDS = frozenset(
    {
        "instagram_ar",
        "facebook_ar",
        "linkedin_ar",
        "x_ar",
        "procom_ar",
        "presence_ar",
        "instauto_ar",
        "start_ar",
        "general_ar",
    }
)

_AI_ACTION_TYPES = frozenset({"ai_type", "best_ai_run_synthesizer"})


def enforcement_enabled() -> bool:
    return (os.environ.get("ENTITLEMENTS_ENFORCEMENT") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _workflows_dir() -> Path:
    try:
        from config.local_paths import agency_root

        return agency_root() / "workflows"
    except Exception:
        return Path("workflows")


def load_workflow_dict(workflow_name: str) -> Optional[dict[str, Any]]:
    path = _workflows_dir() / f"{workflow_name.strip()}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def infer_module_id(wf: dict[str, Any], workflow_name: str) -> str:
    mid = str(wf.get("module_id") or "").strip().lower()
    if mid and mid in MODULE_IDS:
        return mid
    if mid:
        return mid
    return "general_ar"


def infer_requires_ai(wf: dict[str, Any], run_mode: str) -> bool:
    if "requires_ai" in wf and isinstance(wf["requires_ai"], bool):
        return bool(wf["requires_ai"])
    mode = (run_mode or "smart").strip().lower()
    if mode == "smart":
        return True
    steps = wf.get("steps")
    if not isinstance(steps, list):
        return False
    for s in steps:
        if not isinstance(s, dict):
            continue
        at = str(s.get("action_type") or s.get("action") or "").strip().lower()
        if at in _AI_ACTION_TYPES:
            return True
    return False


def _parse_csv_modules(raw: str) -> list[str]:
    return [x.strip().lower() for x in (raw or "").split(",") if x.strip()]


def entitled_modules_from_key(key_doc: dict[str, Any]) -> Optional[list[str]]:
    """
    None = unrestricted (legacy keys with no entitlement field).
    Empty list = explicitly no modules (deny when enforcement on).
    """
    if "entitled_modules" not in key_doc:
        return None
    em = key_doc.get("entitled_modules")
    if em is None:
        return None
    if isinstance(em, list):
        return [str(x).strip().lower() for x in em if str(x).strip()]
    if isinstance(em, str):
        return _parse_csv_modules(em)
    return None


def ai_quota_for_key(key_doc: dict[str, Any]) -> Optional[int]:
    """
    None or 0 = unlimited AI runs for quota purposes.
    Positive int = max AI runs per calendar month.
    """
    if "ai_runs_monthly_quota" not in key_doc:
        return None
    try:
        v = int(key_doc.get("ai_runs_monthly_quota") or 0)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def assert_run_allowed_for_key(
    *,
    key_doc: dict[str, Any],
    workflow_name: str,
    run_mode: str,
    wf: Optional[dict[str, Any]] = None,
) -> None:
    """
    Raises PermissionError with message if run is not allowed.
    No-op when ENTITLEMENTS_ENFORCEMENT is off.
    """
    if not enforcement_enabled():
        return
    wf = wf or load_workflow_dict(workflow_name)
    if wf is None:
        raise PermissionError(f"Workflow '{workflow_name}' not found for entitlement check")
    module_id = infer_module_id(wf, workflow_name)
    requires_ai = infer_requires_ai(wf, run_mode)
    entitled = entitled_modules_from_key(key_doc)
    if entitled is not None and module_id not in entitled and "general_ar" not in entitled:
        raise PermissionError(
            f"Plan does not include module '{module_id}'. Upgrade to unlock this cusear ar™ product."
        )
    if requires_ai:
        quota = ai_quota_for_key(key_doc)
        if quota is not None:
            from agency_api import keys as keys_mod

            used = keys_mod.get_ai_runs_this_month(str(key_doc["_id"]))
            if used >= quota:
                raise PermissionError(
                    f"Monthly AI run quota reached ({used}/{quota}). Add credits or upgrade your cuseai ar™ plan."
                )


def assert_run_allowed_local(
    *,
    workflow_name: str,
    run_mode: str,
    wf: Optional[dict[str, Any]] = None,
) -> None:
    """Trainer/dashboard local runs: ENTITLED_MODULES env CSV; optional TRAINER_AI_RUNS_MONTHLY_CAP."""
    if not enforcement_enabled():
        return
    wf = wf or load_workflow_dict(workflow_name)
    if wf is None:
        raise PermissionError(f"Workflow '{workflow_name}' not found")
    module_id = infer_module_id(wf, workflow_name)
    raw = (os.environ.get("ENTITLED_MODULES") or "").strip()
    if raw:
        allowed = set(_parse_csv_modules(raw))
        if module_id not in allowed and "general_ar" not in allowed:
            raise PermissionError(
                f"This machine's license does not include module '{module_id}'. Contact support to add cusear ar™ products."
            )
    cap_raw = (os.environ.get("TRAINER_AI_RUNS_MONTHLY_CAP") or "").strip()
    if cap_raw and infer_requires_ai(wf, run_mode):
        try:
            cap = int(cap_raw)
        except ValueError:
            cap = 0
        if cap > 0:
            from agency_api import entitlements_local_usage as local_u

            used = local_u.get_local_ai_runs_this_month()
            if used >= cap:
                raise PermissionError(
                    f"Local monthly AI run cap reached ({used}/{cap}). Upgrade or set TRAINER_AI_RUNS_MONTHLY_CAP=0 for unlimited."
                )
