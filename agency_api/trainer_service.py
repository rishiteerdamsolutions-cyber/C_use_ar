"""
Cloud trainer — MongoDB-backed workflows (same JSON shape as dashboard.py file workflows).
Documents: { name, owner_id, data } — scoped per API key when X-API-Key is sent.
Live mouse/keyboard runs only on localhost dashboard.py; this service supports teach + dry-run.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from agency_api.database import Collections, get_collection

logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent.parent

# Browser / public trainer without a key (shared bucket; set TRAINER_REQUIRE_API_KEY=1 to disable)
ANONYMOUS_OWNER = "anonymous"


def _ensure_dash_imports():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


def _parse_multipart(body: bytes, boundary: str) -> dict[str, Any]:
    """
    Parse multipart/form-data payload into field dict.

    Mirrors dashboard.parse_multipart but lives in cloud trainer service so
    Vercel does not need to import dashboard.py (which may touch readonly paths).
    """
    result: dict[str, Any] = {}
    sep = ("--" + boundary).encode()
    for part in body.split(sep)[1:]:
        if not part.strip() or part.strip() == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_bytes, _, data = part.partition(b"\r\n\r\n")
        while data.endswith(b"\r\n"):
            data = data[:-2]
        headers = header_bytes.decode(errors="ignore")
        cd = next((l for l in headers.splitlines() if "Content-Disposition" in l), "")
        nm = re.search(r'name="([^"]+)"', cd)
        fn = re.search(r'filename="([^"]+)"', cd)
        if not nm:
            continue
        name = nm.group(1)
        if fn:
            filename = fn.group(1).strip()
            result.setdefault(name, []).append({"filename": filename, "data": data})
        else:
            result[name] = data.decode(errors="utf-8")
    return result


def _col():
    return get_collection(Collections.TRAINER)


def _list_filter(owner_id: str) -> dict[str, Any]:
    if owner_id == ANONYMOUS_OWNER:
        return {"$or": [{"owner_id": ANONYMOUS_OWNER}, {"owner_id": {"$exists": False}}]}
    return {"owner_id": owner_id}


def _doc_filter(owner_id: str, name: str) -> dict[str, Any]:
    base: dict[str, Any] = {"name": name}
    if owner_id == ANONYMOUS_OWNER:
        return {"$and": [base, {"$or": [{"owner_id": ANONYMOUS_OWNER}, {"owner_id": {"$exists": False}}]}]}
    return {**base, "owner_id": owner_id}


def list_workflows(owner_id: str) -> list[dict[str, Any]]:
    out = []
    for doc in _col().find(_list_filter(owner_id), {"name": 1, "data": 1}):
        d = doc.get("data") or {}
        out.append({"name": doc["name"], "total_steps": d.get("total_steps", len(d.get("steps", [])))})
    return sorted(out, key=lambda x: x["name"])


def get_workflow(owner_id: str, name: str) -> dict[str, Any] | None:
    doc = _col().find_one(_doc_filter(owner_id, name))
    if not doc:
        return None
    return doc.get("data")


def delete_workflow(owner_id: str, name: str) -> bool:
    return _col().delete_one(_doc_filter(owner_id, name)).deleted_count > 0


def _save_workflow_doc(wf: dict[str, Any], owner_id: str) -> None:
    name = wf["workflow_name"]
    wf["total_steps"] = len(wf.get("steps", []))
    flt = _doc_filter(owner_id, name)
    _col().update_one(
        flt,
        {"$set": {"name": name, "owner_id": owner_id, "data": wf}},
        upsert=True,
    )


def delete_step(owner_id: str, wf_name: str, step_num: int) -> tuple[bool, int]:
    wf = get_workflow(owner_id, wf_name)
    if not wf:
        return False, 0
    steps = [s for s in wf.get("steps", []) if s.get("step") != step_num]
    for i, s in enumerate(steps, 1):
        s["step"] = i
    wf["steps"] = steps
    wf["total_steps"] = len(steps)
    _save_workflow_doc(wf, owner_id)
    return True, len(steps)


def process_teach_step(owner_id: str, raw_body: bytes, content_type: str) -> dict[str, Any]:
    bnd = re.search(r"boundary=([^\s;]+)", content_type)
    if not bnd:
        raise ValueError("no multipart boundary")

    fields = _parse_multipart(raw_body, bnd.group(1))
    wf_name = fields.get("workflow_name", "").strip()
    description = fields.get("description", "").strip()
    action_type = fields.get("action_type", "click").strip()
    type_text = fields.get("type_text", "")
    if isinstance(type_text, str):
        type_text = type_text.replace("\r\n", "\n")
    else:
        type_text = ""
    url_field = fields.get("url", "")
    open_url = url_field.strip() if isinstance(url_field, str) else ""

    if not wf_name:
        raise ValueError("workflow_name required")
    if action_type == "type" and not type_text:
        raise ValueError("type_text required for type action")
    if action_type == "open_url" and not open_url:
        raise ValueError("url required for open_url action")
    shell_cmd_f = fields.get("shell_command", "")
    shell_cmd = shell_cmd_f.strip() if isinstance(shell_cmd_f, str) else ""
    if action_type == "shell" and not shell_cmd:
        raise ValueError("shell_command required for shell action")
    if action_type == "wait":
        try:
            _w = float(str(fields.get("wait_seconds", "2") or "2").strip())
        except ValueError as exc:
            raise ValueError("wait_seconds must be a number") from exc
        if not (0.0 <= _w <= 120.0):
            raise ValueError("wait_seconds must be between 0 and 120")

    live_vis = str(fields.get("live_vision", "")).strip().lower() in ("1", "true", "yes")
    screenshots_early = fields.get("screenshot", [])
    if action_type == "click" and not screenshots_early and live_vis and not description:
        raise ValueError("Describe what to click — live vision uses this text at run time")
    if action_type == "click" and not screenshots_early and not live_vis:
        raise ValueError(
            "Click step: upload a training screenshot, or enable “Live screen at run”."
        )

    wf = get_workflow(owner_id, wf_name)
    if not wf:
        wf = {"workflow_name": wf_name, "steps": [], "taught_at": datetime.datetime.utcnow().isoformat()}
    if not isinstance(wf.get("steps"), list):
        wf["steps"] = []
    step_num = len(wf["steps"]) + 1

    step: dict[str, Any] = {
        "step": step_num,
        "action_type": action_type,
        "description": description,
        "x": 0,
        "y": 0,
        "status": "saved",
    }
    if action_type == "type":
        step["type_text"] = type_text
        preview = type_text.replace("\n", " ").strip()
        step["description"] = (preview[:97] + "...") if len(preview) > 100 else (preview or "Type text")
    elif action_type == "open_url":
        step["url"] = open_url
        step["description"] = open_url if len(open_url) <= 120 else open_url[:117] + "..."
    elif action_type == "wait":
        ws_save = float(str(fields.get("wait_seconds", "2") or "2").strip())
        step["wait_seconds"] = ws_save
        note = description.strip()
        step["description"] = (
            (f"Wait {ws_save:g}s — {note}" if note else f"Wait {ws_save:g}s")[:220]
        )
    elif action_type == "shell":
        step["shell_command"] = shell_cmd
        step["description"] = (shell_cmd[:117] + "...") if len(shell_cmd) > 120 else shell_cmd

    if action_type == "click":
        step["live_vision"] = live_vis
        screenshots = fields.get("screenshot", [])
        if screenshots:
            img_data = screenshots[0]["data"]
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            try:
                tmp.write(img_data)
                tmp.flush()
                tmp_path = Path(tmp.name)
            finally:
                tmp.close()
            try:
                step["screenshot"] = f"{wf_name}_step{step_num}.png"
                has_vision_key = bool(
                    (os.environ.get("OPENAI_API_KEY") or "").strip()
                    or (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
                )
                if has_vision_key:
                    try:
                        _ensure_dash_imports()
                        import dashboard as dash

                        coords = dash.analyse_screenshot_for_click(tmp_path, description)
                        step["x"] = int(coords.get("x") or 0)
                        step["y"] = int(coords.get("y") or 0)
                        step["status"] = "analysed"
                    except Exception as ve:
                        logger.warning("Vision failed step %s: %s", step_num, ve)
                        step["status"] = "saved_no_vision"
                else:
                    step["status"] = "saved_no_api_key"
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            step["x"] = 0
            step["y"] = 0
            step["status"] = "live_vision_run"
    elif action_type != "click":
        step["status"] = "saved"

    wf["steps"].append(step)
    wf["total_steps"] = len(wf["steps"])
    _save_workflow_doc(wf, owner_id)

    return {
        "success": True,
        "step": step_num,
        "total_steps": len(wf["steps"]),
        "x": step["x"],
        "y": step["y"],
        "status": step["status"],
    }


def run_dry(owner_id: str, name: str) -> list[dict[str, Any]]:
    wf = get_workflow(owner_id, name)
    if not wf:
        raise FileNotFoundError(f"Workflow '{name}' not found")
    results = []
    for s in wf.get("steps", []):
        action = s.get("action_type") or s.get("action", "click")
        results.append({"step": s.get("step"), "action": action, "status": "dry_run"})
    return results
