"""
Rekky Detect — one workflow step at a time.

For each step: run the action → detect (element attributes or step description) →
tell the Trainer app (step_bridge) → save to workflow JSON → next step.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Optional

from .constants import (
    REKKY_AI_TYPE_SETTLE,
    REKKY_ARROW_POST_PRESS_SETTLE,
    REKKY_ARROW_WAIT,
    REKKY_BETWEEN_STEPS,
    REKKY_CAPTURE_POLL_ATTEMPTS,
    REKKY_CAPTURE_POLL_INTERVAL,
    REKKY_CAPTURE_WAIT,
    REKKY_CHROME_ACTIVATE_WAIT,
    REKKY_DOM_SETTLE,
    REKKY_ENTER_WAIT,
    REKKY_ESCAPE_WAIT,
    REKKY_OPEN_CHROME_SETTLE,
    REKKY_PAGE_READY_POLL,
    REKKY_PAGE_READY_TIMEOUT,
    REKKY_POST_ACTION_SETTLE,
    REKKY_POST_ENTER_SETTLE,
    REKKY_PRE_CAPTURE_SETTLE,
    REKKY_TAB_INTERVAL,
    REKKY_TAB_POST_PRESS_SETTLE,
    REKKY_TYPE_CLEAR_WAIT,
    REKKY_URL_LOAD_WAIT,
    rekky_detect_timing_snapshot,
)
from .os_adapter import element_capture_signal, get_os_adapter, open_url_in_google_chrome
from .rekky import IS_MAC, IS_WINDOWS, _build_anchor_bundle, score_anchor_quality
from .step_bridge import (
    StepBridgeAbort,
    StepBridgeTimeout,
    announce,
    wait_for_ack,
    wait_for_ack_grace,
)
from .workflow import WorkflowJson, WorkflowStep, anchor_has_validation_signal, load_workflow, save_workflow

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[dict[str, Any]], None]]


# Steps with no focusable UI target — app is told the step description instead of element attrs.
REKKY_DESCRIPTION_ONLY_ACTIONS = frozenset(
    {
        "open_url",
        "open_chrome",
        "wait",
        "close_chrome",
        "close_browser",
        "maximise_window",
        "maximize",
    }
)


def _rekky_manual_detect() -> bool:
    return os.getenv("TRAINER_REKKY_MANUAL_DETECT", "").strip().lower() in ("1", "true", "yes")


def _rekky_step_ack_grace() -> float:
    raw = os.getenv("REKKY_STEP_ACK_GRACE", "2.0").strip()
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 2.0


def _rekky_description_only(act: str) -> bool:
    return act in REKKY_DESCRIPTION_ONLY_ACTIONS


def _step_description(step: WorkflowStep, act: str) -> str:
    desc = str(step.get("description") or "").strip()
    if desc:
        return desc
    if act == "open_url":
        return f"Open URL: {str(step.get('url') or '').strip()}"
    if act == "open_chrome":
        return "Open Google Chrome"
    if act == "wait":
        dur = step.get("wait_seconds")
        if dur is None:
            dur = step.get("duration", 1.0)
        return f"Wait {dur}s"
    if act in ("close_chrome", "close_browser"):
        return "Close Chrome / browser"
    if act in ("maximise_window", "maximize"):
        return "Maximise browser window"
    return f"{act.replace('_', ' ')} (step {step.get('step', '?')})"


def _build_description_detect(step: WorkflowStep, act: str, step_num: int) -> dict[str, Any]:
    desc = _step_description(step, act)
    return {
        "detect_kind": "description",
        "step_description": desc,
        "element_at_focus": None,
        "focus_target": None,
        "intermediate_elements": [],
        "mapped_step_preview": {
            "step": step_num,
            "action_type": act,
            "focus_target": None,
            "intermediate_elements": [],
            "step_description": desc,
        },
    }


def _build_step_report(
    *,
    step_num: int,
    step_index: int,
    total_steps: int,
    act: str,
    step: WorkflowStep,
    local_detect: dict[str, Any],
) -> dict[str, Any]:
    kind = str(local_detect.get("detect_kind") or "").strip().lower()
    if not kind:
        kind = "description" if _rekky_description_only(act) else "element"

    if kind == "description":
        desc = str(local_detect.get("step_description") or _step_description(step, act))
        return {
            "step_number": step_num,
            "step_index": step_index,
            "total_steps": total_steps,
            "action_type": act,
            "detect_kind": "description",
            "message": f"Step {step_num}/{total_steps}: {desc}",
            "step_description": desc,
        }

    ann = local_detect.get("focus_target") if isinstance(local_detect.get("focus_target"), dict) else {}
    inter = list(local_detect.get("intermediate_elements") or [])
    tag = str(ann.get("tagName") or "")
    text = str(ann.get("text") or "")[:80]
    role = str(ann.get("role") or "")
    _id = str(ann.get("id") or "")
    quality = str(ann.get("anchor_quality") or "")
    msg = f"Step {step_num}/{total_steps}: detected <{tag or '?'}>"
    if text:
        msg += f' "{text}"'
    if role:
        msg += f" · role={role}"
    if _id:
        msg += f" · id={_id}"
    if len(inter) > 1:
        msg += f" · {len(inter)} tab stops"

    return {
        "step_number": step_num,
        "step_index": step_index,
        "total_steps": total_steps,
        "action_type": act,
        "detect_kind": "element",
        "message": msg,
        "element": {
            "tagName": tag,
            "text": str(ann.get("text") or "")[:100],
            "id": _id,
            "role": role,
            "className": str(ann.get("className") or "")[:120],
            "anchor_quality": quality,
        },
        "intermediate_count": len(inter),
        "intermediates": [
            {
                "position": x.get("position"),
                "tagName": x.get("tagName"),
                "text": (str(x.get("text") or ""))[:60],
                "role": x.get("role"),
            }
            for x in inter[:12]
            if isinstance(x, dict)
        ],
    }


def _local_detect_as_ack(local: dict[str, Any]) -> dict[str, Any]:
    preview = local.get("mapped_step_preview") if isinstance(local.get("mapped_step_preview"), dict) else {}
    ft = preview.get("focus_target") or local.get("focus_target")
    inter = preview.get("intermediate_elements")
    if inter is None:
        inter = local.get("intermediate_elements")
    return {"mapped_step": {"focus_target": ft, "intermediate_elements": inter or []}}


def _find_latest_aha_session_path(workflow_stem: str, workflow_dir: str) -> str | None:
    roots = [
        os.path.abspath(os.path.join(workflow_dir, "..", "logs", "sessions")),
        os.path.abspath(os.path.join(workflow_dir, "..", "..", "logs", "sessions")),
    ]
    found: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        found.extend(glob.glob(os.path.join(root, f"{workflow_stem}*_session.json")))
    if not found:
        return None
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def _prepare_source_workflow(wf_path: str) -> tuple[WorkflowJson, str, str]:
    """
    Copy current JSON to pre_rekky backup and replay from that frozen original.
    If an AHA session clone exists, note it in metadata (steps still from backup = on-disk original).
    """
    wf_path = os.path.abspath(wf_path)
    dirname, fname = os.path.split(wf_path)
    stem, _ext = os.path.splitext(fname) if fname else ("workflow", ".json")
    backup = os.path.join(dirname, f"{stem}_pre_rekky.json")
    shutil.copy2(wf_path, backup)

    session_path = _find_latest_aha_session_path(stem, dirname)
    workflow = load_workflow(backup)
    source_note = "workflow_json_backup"
    if session_path:
        source_note = f"workflow_json_backup (aha_session_ref={os.path.basename(session_path)})"
    workflow["rekky_detect_source"] = source_note
    workflow["rekky_detect_source_path"] = backup
    if session_path:
        workflow["rekky_aha_session_ref"] = session_path
    return workflow, backup, stem


def _ann_from_raw(el: dict[str, Any]) -> dict[str, Any]:
    tag = str(el.get("tagName", "") or "")
    text = str(el.get("text", "") or "")
    _id = str(el.get("id", "") or "")
    role = str(el.get("role", "") or "")
    return {
        "tagName": tag,
        "text": text[:100] if text else "",
        "id": _id,
        "role": role,
        "className": str(el.get("className", "") or "")[:120],
    }


def _finalize_ann(base: dict[str, Any]) -> dict[str, Any]:
    ann = dict(base)
    ann["anchor_quality"] = score_anchor_quality(ann)
    ann["anchor_bundle"] = _build_anchor_bundle(ann)
    return ann


def _type_step_url(step: WorkflowStep) -> str:
    """URL typed in omnibar steps (Trainer `type` + http…) — navigate like open_url."""
    txt = str(step.get("type_text") or "").strip()
    if not txt:
        desc = str(step.get("description") or "").strip()
        if desc.lower().startswith("http"):
            txt = desc
    low = txt.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return txt
    return ""


def _rekky_tab_snapshot_positions(tab_count: int) -> set[int]:
    """Limit AppleScript captures on long Tab runs (e.g. 34 tabs) while still pressing every Tab."""
    try:
        cap = max(3, int((os.getenv("REKKY_DETECT_TAB_SNAPSHOT_MAX") or "12").strip()))
    except ValueError:
        cap = 12
    tc = max(1, int(tab_count))
    if tc <= cap:
        return set(range(1, tc + 1))
    every = max(1, tc // cap)
    positions = set(range(1, tc + 1, every))
    positions.update({1, 2, 3, tc})
    return {p for p in positions if 1 <= p <= tc}


def _empty_tab_intermediate(position: int) -> dict[str, Any]:
    return {
        "position": position,
        "key": "tab",
        "tagName": "",
        "text": "",
        "id": "",
        "role": "",
        "anchor_quality": "WEAK",
        "anchor_bundle": [],
    }


def _rekky_quit_chrome_after_detect() -> bool:
    return os.getenv("TRAINER_REKKY_QUIT_CHROME_AFTER_DETECT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def run_rekky_detect_enrich(
    workflow_path: str,
    *,
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    import pyautogui  # type: ignore

    wf_path = os.path.abspath(workflow_path)
    workflow, backup, stem = _prepare_source_workflow(wf_path)
    adapter = get_os_adapter()
    primary_mod = "command" if IS_MAC else "ctrl"
    pyautogui.FAILSAFE = False
    adapter.keep_display_awake(7200)
    timing = rekky_detect_timing_snapshot()
    workflow["rekky_detect_timing"] = timing
    logger.info("[Rekky Detect] timing profile: %s", timing)

    def _settle_for_capture(*, after_action: bool = True) -> None:
        """Chrome frontmost + DOM/animation settle before reading focus (per step)."""
        adapter.safe_activate_chrome()
        time.sleep(REKKY_CHROME_ACTIVATE_WAIT)
        if after_action:
            time.sleep(REKKY_POST_ACTION_SETTLE)
        time.sleep(REKKY_DOM_SETTLE)
        time.sleep(REKKY_PRE_CAPTURE_SETTLE)
        time.sleep(REKKY_CAPTURE_WAIT)

    try:
        screen = pyautogui.size()
        viewport_profile = {
            "width": int(screen.width),
            "height": int(screen.height),
            "platform": __import__("platform").system(),
        }
    except Exception:
        viewport_profile = {"width": 0, "height": 0, "platform": __import__("platform").system()}

    def _capture_ann_robust(*, after_action: bool = True, fast: bool = False) -> dict[str, Any]:
        """Poll until accessibility/DOM returns a usable anchor or attempts exhaust."""
        if not fast:
            _settle_for_capture(after_action=after_action)
        else:
            adapter.safe_activate_chrome()
            time.sleep(REKKY_TAB_POST_PRESS_SETTLE)
        attempts = max(1, min(int(REKKY_CAPTURE_POLL_ATTEMPTS), 3 if fast else int(REKKY_CAPTURE_POLL_ATTEMPTS)))
        last: dict[str, Any] = {}
        for attempt in range(attempts):
            raw = adapter.capture_active_element() or {}
            last = _ann_from_raw(raw)
            if element_capture_signal(last):
                return _finalize_ann(last)
            if attempt + 1 < attempts:
                time.sleep(REKKY_CAPTURE_POLL_INTERVAL)
        logger.warning(
            "[Rekky Detect] weak capture after %s polls: tag=%r text=%r role=%r",
            attempts,
            last.get("tagName"),
            (last.get("text") or "")[:40],
            last.get("role"),
        )
        return _finalize_ann(last)

    def _snapshot_intermediate(position: int, key: str, *, fast: bool = False) -> dict[str, Any]:
        base = _capture_ann_robust(after_action=True, fast=fast)
        return {
            "position": position,
            "key": key,
            "tagName": base["tagName"],
            "text": base["text"],
            "id": base["id"],
            "role": base["role"],
            "anchor_quality": base["anchor_quality"],
            "anchor_bundle": base["anchor_bundle"],
        }

    def _capture_hint(ann: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": ann.get("tagName") or "",
            "text": (ann.get("text") or "")[:60],
            "role": ann.get("role") or "",
            "quality": ann.get("anchor_quality") or "",
            "has_signal": anchor_has_validation_signal(ann),
        }

    def _build_detect_payload(step: WorkflowStep, act: str, intermediates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        inter = list(intermediates or [])
        if not inter:
            tc = max(1, int(step.get("tab_count", 1)))
            key = "tab" if act == "press_tab" else act.replace("press_", "", 1) or act
            inter = [_snapshot_intermediate(tc if act == "press_tab" else 1, key)]
        ann = _finalize_ann(_ann_from_raw(inter[-1])) if inter else _capture_ann_robust()
        return {
            "element_at_focus": ann,
            "focus_target": dict(ann),
            "intermediate_elements": inter,
            "capture_hint": _capture_hint(ann),
            "mapped_step_preview": {
                "step": step.get("step"),
                "action_type": act,
                "focus_target": ann,
                "intermediate_elements": inter,
            },
        }

    def _detect_press_tab(step: WorkflowStep, *, step_index: int) -> dict[str, Any]:
        adapter.safe_activate_chrome()
        time.sleep(REKKY_CHROME_ACTIVATE_WAIT)
        time.sleep(REKKY_DOM_SETTLE)
        tc = max(1, int(step.get("tab_count", 1)))
        snap_at = _rekky_tab_snapshot_positions(tc)
        inter: list[dict[str, Any]] = []
        for pos in range(1, tc + 1):
            pyautogui.press("tab")
            time.sleep(REKKY_TAB_INTERVAL)
            time.sleep(REKKY_TAB_POST_PRESS_SETTLE)
            if pos in snap_at:
                inter.append(_snapshot_intermediate(pos, "tab", fast=True))
            else:
                inter.append(_empty_tab_intermediate(pos))
            if progress_cb and (pos == 1 or pos == tc or pos % max(1, tc // 8) == 0):
                _report(
                    step,
                    step_index=step_index,
                    bridge_phase="running",
                    extra={
                        "status": "tab_progress",
                        "message": f"Tab {pos}/{tc} (capturing {len(snap_at)} focus snapshots)",
                        "tab_position": pos,
                        "tab_count": tc,
                    },
                )
        return _build_detect_payload(step, "press_tab", inter)

    def _detect_press_arrow(step: WorkflowStep) -> dict[str, Any]:
        adapter.safe_activate_chrome()
        time.sleep(REKKY_CHROME_ACTIVATE_WAIT)
        time.sleep(REKKY_DOM_SETTLE)
        cnt = max(1, int(step.get("count", 1)))
        direction = str(step.get("direction", "down"))
        key = f"arrow_{direction}"
        inter: list[dict[str, Any]] = []
        for pos in range(1, cnt + 1):
            pyautogui.press(direction)
            time.sleep(REKKY_ARROW_WAIT)
            time.sleep(REKKY_ARROW_POST_PRESS_SETTLE)
            inter.append(_snapshot_intermediate(pos, key))
        return _build_detect_payload(step, "press_arrow", inter)

    def _apply_detect_to_step(step: WorkflowStep, act: str, ack: dict[str, Any], local: dict[str, Any]) -> None:
        mapped = ack.get("mapped_step") if isinstance(ack.get("mapped_step"), dict) else {}
        if mapped.get("focus_target"):
            step["focus_target"] = mapped["focus_target"]
        elif local.get("focus_target"):
            step["focus_target"] = local["focus_target"]
        if mapped.get("intermediate_elements") is not None:
            step["intermediate_elements"] = mapped["intermediate_elements"]
        elif local.get("intermediate_elements") is not None:
            step["intermediate_elements"] = local["intermediate_elements"]
        elif act in ("open_url", "open_chrome", "wait", "type", "type_whatsapp_number", "type_completion_message"):
            step["intermediate_elements"] = []
            step["focus_target"] = step.get("focus_target")
        hint = local.get("capture_hint")
        if isinstance(hint, dict):
            step["rekky_capture_hint"] = hint
        if str(local.get("detect_kind") or "") == "description":
            step["rekky_step_description"] = str(local.get("step_description") or "")
        report = local.get("step_report")
        if isinstance(report, dict):
            step["rekky_step_report"] = report
        if act == "open_url" and not step.get("checkpoint_signature"):
            step["checkpoint_signature"] = {
                "kind": "page",
                "url_hint": str(step.get("url") or ""),
                "landmark_hint": "open_url",
                "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        if step.get("is_checkpoint") and not step.get("checkpoint_signature"):
            ann = _capture_ann_robust()
            step["checkpoint_signature"] = {
                "kind": "checkpoint",
                "landmark_hint": str(ann.get("text") or ann.get("role") or ""),
                "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    def _wait_secs(st: dict[str, Any]) -> float:
        if st.get("wait_seconds") is not None:
            return float(st["wait_seconds"])
        return float(st.get("duration", 1.0))

    def _launch_chrome_for_enrich() -> None:
        if IS_MAC:
            subprocess.Popen(["open", "-a", "Google Chrome"])
        elif IS_WINDOWS:
            subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
        else:
            subprocess.Popen(["google-chrome"])
        time.sleep(REKKY_OPEN_CHROME_SETTLE)

    def _wait_page_ready() -> None:
        ready = adapter.wait_for_page_ready(REKKY_PAGE_READY_TIMEOUT, REKKY_PAGE_READY_POLL)
        if not ready:
            logger.warning("[Rekky Detect] page ready timeout after %.0fs", REKKY_PAGE_READY_TIMEOUT)
        time.sleep(REKKY_DOM_SETTLE)

    def _execute_step(step: WorkflowStep, act: str) -> None:
        if act in ("press_tab", "press_arrow"):
            return
        if act == "open_url":
            open_url_in_google_chrome(str(step.get("url") or ""))
            time.sleep(REKKY_URL_LOAD_WAIT)
            adapter.activate_chrome()
            _wait_page_ready()
            pyautogui.press("escape", presses=2)
            time.sleep(REKKY_ESCAPE_WAIT)
            if not IS_MAC:
                pyautogui.hotkey("win", "up")
            time.sleep(0.5)
            pyautogui.press("escape", presses=2)
            time.sleep(REKKY_ESCAPE_WAIT)
            return
        if act == "open_chrome":
            _launch_chrome_for_enrich()
            adapter.activate_chrome()
            time.sleep(REKKY_DOM_SETTLE)
            return
        if act == "type":
            nav_url = _type_step_url(step)
            if nav_url:
                open_url_in_google_chrome(nav_url)
                time.sleep(REKKY_URL_LOAD_WAIT)
                adapter.activate_chrome()
                _wait_page_ready()
                pyautogui.press("escape", presses=2)
                time.sleep(REKKY_ESCAPE_WAIT)
                if not IS_MAC:
                    pyautogui.hotkey("win", "up")
                time.sleep(0.35)
                return
            adapter.safe_activate_chrome()
            time.sleep(REKKY_DOM_SETTLE)
            txt = str(step.get("type_text") or "").strip()
            if not txt and str(step.get("description") or "").strip().lower().startswith("http"):
                txt = str(step.get("description") or "").strip()
            pyautogui.hotkey(primary_mod, "l")
            time.sleep(0.35)
            if txt:
                pyautogui.hotkey(primary_mod, "a")
                time.sleep(0.08)
                pyautogui.press("backspace")
                pyautogui.write(txt, interval=0.015)
            time.sleep(REKKY_CAPTURE_WAIT)
            return
        if act == "type_whatsapp_number":
            adapter.safe_activate_chrome()
            time.sleep(REKKY_DOM_SETTLE)
            wn = str(step.get("whatsapp_number") or step.get("type_text") or "5550000000").strip()
            pyautogui.write(wn, interval=0.02)
            time.sleep(REKKY_CAPTURE_WAIT)
            return
        if act == "type_completion_message":
            adapter.safe_activate_chrome()
            time.sleep(REKKY_DOM_SETTLE)
            pyautogui.write("REKKY_COMPLETION_PLACEHOLDER", interval=0.02)
            time.sleep(REKKY_CAPTURE_WAIT)
            return
        if act == "close_chrome":
            try:
                if IS_MAC:
                    subprocess.run(
                        ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                        timeout=15,
                        capture_output=True,
                    )
                elif IS_WINDOWS:
                    subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], timeout=8, capture_output=True)
            except Exception:
                pass
            time.sleep(0.8)
            return
        if act == "press_enter":
            adapter.safe_activate_chrome()
            time.sleep(REKKY_DOM_SETTLE)
            pyautogui.press("enter")
            time.sleep(REKKY_ENTER_WAIT)
            time.sleep(REKKY_POST_ENTER_SETTLE)
            if step.get("is_final") or step.get("is_destination"):
                pyautogui.press("escape", presses=2)
                time.sleep(REKKY_ESCAPE_WAIT)
            return
        if act == "press_escape":
            cnt_e = max(1, int(step.get("count", 1)))
            for _ in range(cnt_e):
                pyautogui.press("escape")
                time.sleep(REKKY_ESCAPE_WAIT)
            return
        if act == "press_space":
            cnt_s = max(1, int(step.get("count", 1)))
            for _ in range(cnt_s):
                pyautogui.press("space")
                time.sleep(REKKY_ESCAPE_WAIT)
            return
        if act == "press_home":
            adapter.safe_activate_chrome()
            pyautogui.press("home")
            time.sleep(REKKY_CAPTURE_WAIT)
            return
        if act == "press_end":
            adapter.safe_activate_chrome()
            pyautogui.press("end")
            time.sleep(REKKY_CAPTURE_WAIT)
            return
        if act == "hotkey":
            adapter.safe_activate_chrome()
            keys = step.get("keys") or ()
            pyautogui.hotkey(*keys)
            time.sleep(REKKY_CAPTURE_WAIT)
            return
        if act in {"type_text", "ai_type"}:
            adapter.safe_activate_chrome()
            time.sleep(REKKY_DOM_SETTLE)
            pyautogui.typewrite("REKKY_TEST", interval=0.05)
            time.sleep(REKKY_AI_TYPE_SETTLE)
            pyautogui.hotkey(primary_mod, "a")
            time.sleep(0.12)
            pyautogui.press("delete")
            time.sleep(REKKY_TYPE_CLEAR_WAIT)
            return
        if act in ("maximise_window", "maximize"):
            if not IS_MAC:
                pyautogui.hotkey("win", "up")
            time.sleep(0.5)
            return
        if act == "wait":
            time.sleep(_wait_secs(step))
            time.sleep(REKKY_POST_ACTION_SETTLE)
            return

    step_list = [s for s in (workflow.get("steps") or []) if isinstance(s, dict)]
    total_steps = len(step_list)
    wf_display_name = str(workflow.get("workflow_name") or stem or "").strip()
    elems = 0
    steps_processed = 0
    weak_captures = 0

    def _report(step_obj: WorkflowStep, *, step_index: int, bridge_phase: str, extra: dict | None = None) -> None:
        if not progress_cb:
            return
        payload = {
            "current_step": step_index,
            "total_steps": total_steps,
            "action_type": str(step_obj.get("action_type") or "").strip(),
            "step_number": step_obj.get("step"),
            "elements_captured": elems,
            "steps_enriched": steps_processed,
            "bridge_phase": bridge_phase,
        }
        if extra:
            payload.update(extra)
        try:
            progress_cb(payload)
        except Exception:
            pass

    try:
        for step_index, step in enumerate(step_list, start=1):
            act = str(step.get("action_type") or "").strip()
            step_num = int(step.get("step") or step_index)
            _report(step, step_index=step_index, bridge_phase="running")

            local_detect: dict[str, Any] | None = None
            try:
                if act == "press_tab":
                    local_detect = _detect_press_tab(step, step_index=step_index)
                elif act == "press_arrow":
                    local_detect = _detect_press_arrow(step)
                else:
                    _execute_step(step, act)

                if local_detect is None:
                    if act in ("close_browser",) or _rekky_description_only(act):
                        local_detect = _build_description_detect(step, act, step_num)
                    elif act == "type" and _type_step_url(step):
                        local_detect = _build_description_detect(step, act, step_num)
                        local_detect["step_description"] = f"Navigate: {_type_step_url(step)}"
                    else:
                        local_detect = _build_detect_payload(step, act)

                step_report = _build_step_report(
                    step_num=step_num,
                    step_index=step_index,
                    total_steps=total_steps,
                    act=act,
                    step=step,
                    local_detect=local_detect,
                )
                local_detect["step_report"] = step_report

                inter_count = len(local_detect.get("intermediate_elements") or [])
                elems += inter_count
                hint = local_detect.get("capture_hint")
                if step_report.get("detect_kind") == "element" and isinstance(hint, dict) and not hint.get(
                    "has_signal"
                ):
                    weak_captures += 1

                _report(
                    step,
                    step_index=step_index,
                    bridge_phase="detected",
                    extra={
                        "status": "step_detected",
                        "message": step_report.get("message"),
                        "step_report": step_report,
                        **local_detect,
                    },
                )
                detected_token = announce(
                    phase="rekky",
                    bridge_phase="detected",
                    workflow_name=wf_display_name,
                    step_index=step_index,
                    total_steps=total_steps,
                    step_number=step_num,
                    action_type=act,
                    extra={
                        "status": "step_detected",
                        "message": step_report.get("message"),
                        "step_report": step_report,
                        "rekky_manual": _rekky_manual_detect(),
                        **local_detect,
                    },
                )
                if _rekky_manual_detect():
                    ack_detect = wait_for_ack(detected_token)
                else:
                    wait_for_ack_grace(detected_token, grace_sec=_rekky_step_ack_grace())
                    ack_detect = _local_detect_as_ack(local_detect)
            except (StepBridgeAbort, StepBridgeTimeout) as exc:
                raise RuntimeError(str(exc)) from exc

            _apply_detect_to_step(step, act, ack_detect, local_detect)
            step["rekky_step_report"] = step_report
            steps_processed += 1
            _report(
                step,
                step_index=step_index,
                bridge_phase="saved",
                extra={"status": "step_saved", "message": step_report.get("message"), "step_report": step_report},
            )
            save_workflow(wf_path, workflow)
            if step_index < total_steps:
                time.sleep(REKKY_BETWEEN_STEPS)

        workflow.setdefault("workflow_name", stem)
        workflow["rekky_enriched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        workflow["rekky_steps_enriched"] = steps_processed
        workflow["rekky_elements_captured"] = elems
        workflow["rekky_weak_captures"] = weak_captures
        workflow["viewport_profile"] = viewport_profile
        save_workflow(wf_path, workflow)

        if _rekky_quit_chrome_after_detect():
            try:
                if IS_MAC:
                    subprocess.run(
                        ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                        timeout=15,
                        capture_output=True,
                    )
                elif IS_WINDOWS:
                    subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], timeout=8, capture_output=True)
            except Exception:
                pass

        return {
            "workflow_path": wf_path,
            "backup_path": backup,
            "steps_enriched": steps_processed,
            "elements_captured": elems,
            "weak_captures": weak_captures,
            "source": workflow.get("rekky_detect_source", ""),
        }
    finally:
        adapter.stop_keep_awake()
