from __future__ import annotations

import argparse
import json
import os
import platform as _platform
import shutil
import threading
import time
from typing import Any

from .constants import (
    REKKY_ARROW_WAIT,
    REKKY_CAPTURE_WAIT,
    REKKY_ENTER_WAIT,
    REKKY_ESCAPE_WAIT,
    REKKY_TAB_INTERVAL,
    REKKY_TYPE_CLEAR_WAIT,
    REKKY_URL_LOAD_WAIT,
)
from .os_adapter import get_os_adapter, open_url_in_google_chrome
from .workflow import WorkflowJson, WorkflowStep, load_workflow, save_workflow, score_anchor

IS_MAC = _platform.system() == "Darwin"
IS_WINDOWS = _platform.system() == "Windows"


def score_anchor_quality(element: dict[str, Any]) -> str:
    """Rekky enrichment anchor scoring (prompt Part 7)."""

    id_ = str(element.get("id", "") or "").strip()
    role = str(element.get("role", "") or "").strip()
    text = str(element.get("text", "") or "").strip()

    if id_:
        return "STRONG"
    if role and len(text) < 20:
        return "STRONG"
    if role and not text:
        return "MEDIUM"
    if len(text) < 30:
        return "MEDIUM"
    return "WEAK"


def _rekky_run_macos_keyloop(
    on_key_name: Any,
    *,
    stop_key: str = "f10",
    stop_event: threading.Event | None = None,
) -> None:
    """
    macOS key capture without sudo using Quartz event tap.

    Requires Accessibility permission, but does not require running as root.
    """
    import Quartz  # type: ignore
    import CoreFoundation  # type: ignore

    # KeyCode mapping (US keyboard); sufficient for our limited key set.
    keycode_to_name = {
        48: "tab",
        36: "enter",
        53: "escape",
        49: "space",
        115: "home",
        119: "end",
        123: "left",
        124: "right",
        125: "down",
        126: "up",
        109: "f10",  # Mac: F10 keycode commonly 109
    }

    stop = {"done": False}
    stop_event = stop_event or threading.Event()

    def _callback(proxy, event_type, event, refcon):
        try:
            if stop_event.is_set():
                CoreFoundation.CFRunLoopStop(CoreFoundation.CFRunLoopGetCurrent())
                return event
            if event_type != Quartz.kCGEventKeyDown:
                return event
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            name = keycode_to_name.get(int(code))
            if not name:
                return event
            if name == stop_key:
                stop["done"] = True
                CoreFoundation.CFRunLoopStop(CoreFoundation.CFRunLoopGetCurrent())
                return event

            # Emulate keyboard library event shape (avoid Python class-scope name capture bugs)
            e = type("_RekkyEvent", (), {"event_type": "down", "name": name})()
            on_key_name(e)
        except Exception:
            pass
        return event

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,
        mask,
        _callback,
        None,
    )
    if not tap:
        raise RuntimeError(
            "Could not create macOS event tap. "
            "Enable Accessibility for Terminal/Cursor in System Settings → Privacy & Security → Accessibility."
        )

    run_loop_source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    CoreFoundation.CFRunLoopAddSource(
        CoreFoundation.CFRunLoopGetCurrent(),
        run_loop_source,
        CoreFoundation.kCFRunLoopCommonModes,
    )
    Quartz.CGEventTapEnable(tap, True)

    # Poll stop_event so "Stop Recording" works immediately even if user stops pressing keys.
    def _tick(_timer, _info):
        try:
            if stop_event.is_set():
                CoreFoundation.CFRunLoopStop(CoreFoundation.CFRunLoopGetCurrent())
        except Exception:
            pass

    timer = CoreFoundation.CFRunLoopTimerCreate(
        None,
        CoreFoundation.CFAbsoluteTimeGetCurrent() + 0.1,
        0.1,
        0,
        0,
        _tick,
        None,
    )
    CoreFoundation.CFRunLoopAddTimer(
        CoreFoundation.CFRunLoopGetCurrent(),
        timer,
        CoreFoundation.kCFRunLoopCommonModes,
    )

    CoreFoundation.CFRunLoopRun()


def _rekky_run_generic_keyboard(
    on_key_name: Any,
    *,
    stop_key: str = "f10",
    stop_event: threading.Event | None = None,
) -> None:
    import keyboard  # type: ignore

    stop_event = stop_event or threading.Event()
    keyboard.hook(on_key_name)
    # Polling wait so an external stop can end recording
    while True:
        if stop_event.is_set():
            break
        if keyboard.is_pressed(stop_key):
            break
        time.sleep(0.05)
    keyboard.unhook_all()


def enrich_workflow(workflow_path: str) -> dict[str, Any]:
    """
    Replay an existing workflow in Chrome slowly and persist `intermediate_elements`/`focus_target`
    anchors (macOS: os_adapter active-element probe; Windows: UIA focused element — install
    requirements-rekky.txt for comtypes on Windows, pyobjc on macOS for recording).
    """
    import subprocess

    import pyautogui  # type: ignore

    wf_path = os.path.abspath(workflow_path)
    workflow = load_workflow(wf_path)
    adapter = get_os_adapter()
    dirname, fname = os.path.split(wf_path)
    stem, _ext = os.path.splitext(fname) if fname else ("workflow", ".json")

    backup = os.path.join(dirname, f"{stem}_pre_rekky.json")
    shutil.copy2(wf_path, backup)

    def _capture_windows_element() -> dict[str, Any]:
        try:
            import comtypes.client  # type: ignore

            uia = comtypes.client.CreateObject("UIAutomationClient.CUIAutomation")
            elem = uia.GetFocusedElement()
            if not elem:
                return {}
            name = (elem.CurrentName or "").strip()
            auto_id = (elem.CurrentAutomationId or "").strip()
            role = (elem.CurrentLocalizedControlType or "").strip().lower()
            tag = "div"
            if "edit" in role or "text" in role:
                tag = "input"
            if "button" in role:
                tag = "button"
                role = "button"
            return {
                "tagName": tag,
                "text": name[:100],
                "id": auto_id,
                "role": role[:50],
            }
        except Exception:
            return {}

    def _capture_ann() -> dict[str, Any]:
        el = _capture_windows_element() if IS_WINDOWS else adapter.capture_active_element()
        tag = el.get("tagName", "") or ""
        text = el.get("text", "") or ""
        _id = el.get("id", "") or ""
        role = el.get("role", "") or ""
        return {
            "tagName": tag,
            "text": text[:100] if text else "",
            "id": _id,
            "role": role,
        }

    def _snapshot_intermediate(position: int, key: str) -> dict[str, Any]:
        base = _capture_ann()
        base["anchor_quality"] = score_anchor_quality(base)
        return {
            "position": position,
            "key": key,
            "tagName": base["tagName"],
            "text": base["text"],
            "id": base["id"],
            "role": base["role"],
            "anchor_quality": base["anchor_quality"],
        }

    elems = 0
    steps_processed = 0
    pyautogui.FAILSAFE = False
    primary_mod = "command" if IS_MAC else "ctrl"

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
        time.sleep(1.5)

    for step in workflow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        act = str(step.get("action_type") or "").strip()

        if act == "open_url":
            open_url_in_google_chrome(str(step.get("url") or ""))
            time.sleep(REKKY_URL_LOAD_WAIT)
            adapter.activate_chrome()
            pyautogui.press("escape", presses=2)
            time.sleep(REKKY_ESCAPE_WAIT)
            if IS_MAC:
                pyautogui.hotkey("command", "control", "f")
            else:
                pyautogui.hotkey("win", "up")
            time.sleep(0.5)
            pyautogui.press("escape", presses=2)
            time.sleep(REKKY_ESCAPE_WAIT)
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "open_chrome":
            _launch_chrome_for_enrich()
            adapter.activate_chrome()
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "type":
            # Omnibar URL / text — matches Trainer open_chrome → type → enter flows.
            adapter.safe_activate_chrome()
            time.sleep(0.35)
            txt = str(step.get("type_text") or "").strip()
            if not txt and str(step.get("description") or "").strip().lower().startswith("http"):
                txt = str(step.get("description") or "").strip()
            pyautogui.hotkey(primary_mod, "l")
            time.sleep(0.28)
            if txt:
                pyautogui.hotkey(primary_mod, "a")
                time.sleep(0.06)
                pyautogui.press("backspace")
                pyautogui.write(txt, interval=0.015)
            time.sleep(REKKY_CAPTURE_WAIT)
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "type_whatsapp_number":
            adapter.safe_activate_chrome()
            time.sleep(0.25)
            wn = str(step.get("whatsapp_number") or step.get("type_text") or "5550000000").strip()
            pyautogui.write(wn, interval=0.02)
            time.sleep(REKKY_CAPTURE_WAIT)
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "type_completion_message":
            adapter.safe_activate_chrome()
            time.sleep(0.25)
            pyautogui.write("REKKY_COMPLETION_PLACEHOLDER", interval=0.02)
            time.sleep(REKKY_CAPTURE_WAIT)
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

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
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "press_tab":
            ims: list[dict[str, Any]] = []
            tc = max(1, int(step.get("tab_count", 1)))
            for i in range(1, tc + 1):
                pyautogui.press("tab")
                time.sleep(REKKY_TAB_INTERVAL)
                time.sleep(REKKY_CAPTURE_WAIT)
                ims.append(_snapshot_intermediate(i, "tab"))
                elems += 1
            step["intermediate_elements"] = ims
            step["focus_target"] = dict(ims[-1]) if ims else None
            steps_processed += 1
            continue

        if act == "press_enter":
            pyautogui.press("enter")
            time.sleep(REKKY_ENTER_WAIT)
            inter = [_snapshot_intermediate(1, "enter")]
            elems += 1
            step["intermediate_elements"] = inter
            step["focus_target"] = dict(inter[0])

            if step.get("is_final") or step.get("is_destination"):
                pyautogui.press("escape", presses=2)
                time.sleep(REKKY_ESCAPE_WAIT)
            steps_processed += 1
            continue

        if act == "press_arrow":
            cnt = max(1, int(step.get("count", 1)))
            direction = str(step.get("direction", "down"))
            keyname = {"up": "arrow_up", "down": "arrow_down", "left": "arrow_left", "right": "arrow_right"}.get(
                direction,
                "arrow_" + direction,
            )
            ims_arr: list[dict[str, Any]] = []
            for i in range(1, cnt + 1):
                pyautogui.press(direction)
                time.sleep(REKKY_ARROW_WAIT)
                ims_arr.append(_snapshot_intermediate(i, keyname))
                elems += 1
            step["intermediate_elements"] = ims_arr
            step["focus_target"] = dict(ims_arr[-1]) if ims_arr else None
            steps_processed += 1
            continue

        if act == "press_escape":
            ims_esc: list[dict[str, Any]] = []
            cnt_e = max(1, int(step.get("count", 1)))
            for i in range(1, cnt_e + 1):
                pyautogui.press("escape")
                time.sleep(REKKY_ESCAPE_WAIT)
                ims_esc.append(_snapshot_intermediate(i, "escape"))
                elems += 1
            step["intermediate_elements"] = ims_esc
            step["focus_target"] = dict(ims_esc[-1]) if ims_esc else None
            steps_processed += 1
            continue

        if act == "press_space":
            ims_sp: list[dict[str, Any]] = []
            cnt_s = max(1, int(step.get("count", 1)))
            for i in range(1, cnt_s + 1):
                pyautogui.press("space")
                time.sleep(REKKY_ESCAPE_WAIT)
                ims_sp.append(_snapshot_intermediate(i, "space"))
                elems += 1
            step["intermediate_elements"] = ims_sp
            step["focus_target"] = dict(ims_sp[-1]) if ims_sp else None
            steps_processed += 1
            continue

        if act == "press_home":
            pyautogui.press("home")
            time.sleep(REKKY_CAPTURE_WAIT)
            inter_h = [_snapshot_intermediate(1, "home")]
            elems += 1
            step["intermediate_elements"] = inter_h
            step["focus_target"] = dict(inter_h[0])
            steps_processed += 1
            continue

        if act == "press_end":
            pyautogui.press("end")
            time.sleep(REKKY_CAPTURE_WAIT)
            inter_e = [_snapshot_intermediate(1, "end")]
            elems += 1
            step["intermediate_elements"] = inter_e
            step["focus_target"] = dict(inter_e[0])
            steps_processed += 1
            continue

        if act == "hotkey":
            keys = step.get("keys") or ()
            lbl = "+".join(str(k).strip().lower() for k in keys if k)
            pyautogui.hotkey(*keys)
            time.sleep(REKKY_CAPTURE_WAIT)
            inter_hk = [_snapshot_intermediate(1, lbl or "hotkey")]
            elems += 1
            step["intermediate_elements"] = inter_hk
            step["focus_target"] = dict(inter_hk[0])
            steps_processed += 1
            continue

        if act in {"type_text", "ai_type"}:
            pyautogui.typewrite("REKKY_TEST", interval=0.05)
            time.sleep(REKKY_CAPTURE_WAIT)
            pyautogui.hotkey(primary_mod, "a")
            time.sleep(0.08)
            pyautogui.press("delete")
            time.sleep(REKKY_TYPE_CLEAR_WAIT)
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act in ("maximise_window", "maximize"):
            if IS_MAC:
                pyautogui.hotkey("command", "control", "f")
            else:
                pyautogui.hotkey("win", "up")
            time.sleep(0.5)
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "wait":
            time.sleep(_wait_secs(step))
            step["intermediate_elements"] = []
            step["focus_target"] = None
            steps_processed += 1
            continue

        if act == "close_browser":
            step["intermediate_elements"] = []
            step["focus_target"] = None
            continue

        step.setdefault("intermediate_elements", [])
        step.setdefault("focus_target", step.get("focus_target"))

    workflow.setdefault("workflow_name", stem)
    workflow["rekky_enriched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    workflow["rekky_steps_enriched"] = steps_processed
    workflow["rekky_elements_captured"] = elems
    save_workflow(wf_path, workflow)

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
    }


def start_rekky_recording(
    *,
    workflow_name: str,
    platform: str,
    url: str,
    workflows_dir: str,
    stop_event: threading.Event | None = None,
    batch_tabs: bool = True,
) -> WorkflowJson:
    """
    Training-only recorder.

    Records operator keyboard input and active-element anchors into a workflow JSON.
    Output schema matches the v2 spec in the prompt.

    batch_tabs: When True (legacy), consecutive Tab keys accumulate into one press_tab
    step. When False (per-keypress / recommended), each Tab is its own step with tab_count 1.
    """

    os.makedirs(workflows_dir, exist_ok=True)

    os_adapter = get_os_adapter()
    steps: list[WorkflowStep] = []
    tab_count = 0
    arrow_buffer: dict[str, Any] = {"key": None, "count": 0}
    step_number = [1]

    def capture() -> dict[str, Any]:
        el = os_adapter.capture_active_element()
        if el:
            el["anchor_quality"] = score_anchor(el)
        return el

    def flush_tabs() -> None:
        nonlocal tab_count
        if tab_count > 0:
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_tab",
                    "tab_count": tab_count,
                    "focus_target": capture(),
                    "wait": 0.5,
                }
            )
            step_number[0] += 1
            tab_count = 0

    def flush_arrows() -> None:
        if arrow_buffer["count"] > 0:
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_arrow",
                    "direction": arrow_buffer["key"],
                    "count": arrow_buffer["count"],
                    "focus_target": capture(),
                    "wait": 0.3,
                }
            )
            step_number[0] += 1
            arrow_buffer["key"] = None
            arrow_buffer["count"] = 0

    def on_key(event: Any) -> None:
        nonlocal tab_count
        if getattr(event, "event_type", "") != "down":
            return

        key = str(getattr(event, "name", "")).lower()

        if key == "tab":
            flush_arrows()
            if not batch_tabs:
                steps.append(
                    {
                        "step": step_number[0],
                        "action_type": "press_tab",
                        "tab_count": 1,
                        "focus_target": capture(),
                        "wait": 0.5,
                    }
                )
                step_number[0] += 1
            else:
                tab_count += 1
            return

        if key in ["up", "down", "left", "right"]:
            flush_tabs()
            if arrow_buffer["key"] == key:
                arrow_buffer["count"] += 1
            else:
                flush_arrows()
                arrow_buffer["key"] = key
                arrow_buffer["count"] = 1
            return

        if key == "enter":
            flush_tabs()
            flush_arrows()
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_enter",
                    "is_destination": False,
                    "is_final": False,
                    "focus_target": capture(),
                    "wait": 1.0,
                }
            )
            step_number[0] += 1
            return

        if key == "space":
            flush_tabs()
            flush_arrows()
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_space",
                    "count": 1,
                    "focus_target": capture(),
                    "wait": 0.3,
                }
            )
            step_number[0] += 1
            return

        if key == "home":
            flush_tabs()
            flush_arrows()
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_home",
                    "focus_target": None,
                    "wait": 0.3,
                }
            )
            step_number[0] += 1
            return

        if key == "end":
            flush_tabs()
            flush_arrows()
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_end",
                    "focus_target": None,
                    "wait": 0.3,
                }
            )
            step_number[0] += 1
            return

        if key == "escape":
            flush_tabs()
            flush_arrows()
            steps.append(
                {
                    "step": step_number[0],
                    "action_type": "press_escape",
                    "count": 1,
                    "focus_target": None,
                    "wait": 0.4,
                }
            )
            step_number[0] += 1
            return

        # Rekky can be extended to capture hotkeys/type_text/ai_type/maximise_window/etc.
        # Those are intentionally left to higher-level trainer UX in this repo.

    stop_event = stop_event or threading.Event()
    if _platform.system() == "Darwin":
        keyloop = lambda: _rekky_run_macos_keyloop(on_key, stop_key="f10", stop_event=stop_event)
    else:
        keyloop = lambda: _rekky_run_generic_keyboard(on_key, stop_key="f10", stop_event=stop_event)

    # Add opening step automatically
    steps.append(
        {
            "step": 0,
            "action_type": "open_url",
            "url": url,
            "wait": 4.0,
            "focus_target": None,
        }
    )

    print(f"[Rekky] Recording started — {workflow_name}")
    print("[Rekky] Navigate the workflow now. Press F10 to stop (or use Trainer Stop).")

    keyloop()

    flush_tabs()
    flush_arrows()

    # Renumber steps 1..N
    for i, s in enumerate(steps):
        s["step"] = i + 1

    workflow: WorkflowJson = {
        "workflow_name": workflow_name,
        "platform": platform,
        "engine": "wra_v2",
        "version": "1.0",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_steps": len(steps),
        "steps": steps,
    }

    path = os.path.join(workflows_dir, f"{workflow_name}.json")
    save_workflow(path, workflow)

    print(f"[Rekky] Recording saved — {len(steps)} steps — {path}")
    return workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Rekky (WRA v2) recorder & enrichment utility")
    parser.add_argument("--enrich", metavar="PATH", help="Replay an existing workflow JSON to enrich anchors (macOS Chrome)")
    parser.add_argument("--name", required=False, help="Workflow name for recording mode")
    parser.add_argument("--platform", required=False, help="Platform name for recording mode")
    parser.add_argument("--url", required=False, help="Starting URL for recording mode")
    parser.add_argument(
        "--workflows-dir",
        default=os.path.join(os.getcwd(), "workflows"),
        help="Output dir for recordings (recording mode)",
    )
    args = parser.parse_args()

    if args.enrich:
        rep = enrich_workflow(os.path.abspath(args.enrich))
        print(json.dumps(rep, indent=2))
        return

    if not args.name or not args.platform or not args.url:
        parser.error("--name, --platform, and --url are required unless using --enrich")

    start_rekky_recording(
        workflow_name=str(args.name).strip(),
        platform=str(args.platform).strip(),
        url=str(args.url).strip(),
        workflows_dir=args.workflows_dir,
    )


if __name__ == "__main__":
    main()


