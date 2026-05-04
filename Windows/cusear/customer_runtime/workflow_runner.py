"""Workflow execution for the runner-only customer app.

This module deliberately does not import dashboard.py. It executes shipped JSON
workflows from AGENCY_HOME/workflows and records audits locally.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from threading import Event
from typing import Any, Callable

from .audit import save_run_audit
from .notifications import send_whatsapp_confirmation
from .paths import load_bundle, load_json, root, workflow_path

StatusCallback = Callable[[str], None]


@dataclasses.dataclass
class WorkflowRunResult:
    name: str
    ok: bool
    audit_path: str
    error: str = ""


def _emit(cb: StatusCallback | None, message: str) -> None:
    if cb:
        cb(message)


def _substitute(text: Any, variables: dict[str, str]) -> str:
    result = str(text or "")
    for key, value in variables.items():
        result = result.replace(str(key), str(value))
    return result


def _action_for_step(step: dict[str, Any]) -> dict[str, Any]:
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    merged = dict(step)
    merged.update(action)
    return merged


def _press_key(executor: Any, key: str, count: int = 1) -> None:
    pg = executor._pg
    old_pause = getattr(pg, "PAUSE", 0.1)
    try:
        pg.PAUSE = 0
        pg.press(key, presses=max(1, int(count or 1)), interval=0)
    finally:
        pg.PAUSE = old_pause


def _open_chrome() -> None:
    sys_name = platform.system()
    if sys_name == "Darwin":
        subprocess.Popen(["open", "-a", "Google Chrome"])
    elif sys_name == "Windows":
        subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
    else:
        subprocess.Popen(["google-chrome"])
    time.sleep(1.5)


def _close_chrome() -> None:
    sys_name = platform.system()
    if sys_name == "Darwin":
        subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to quit'], check=False)
    elif sys_name == "Windows":
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], check=False, capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "chrome"], check=False, capture_output=True)
    time.sleep(1.0)


def _maximize_window(executor: Any) -> None:
    if platform.system() == "Darwin":
        executor.shortcut("ctrl", "command", "f")
    else:
        executor.shortcut("win", "up")


def _ensure_url(value: str) -> str:
    url = str(value or "").strip()
    if url and not re.match(r"^[a-zA-Z][-a-zA-Z0-9+.]*:", url):
        url = "https://" + url.lstrip("/")
    return url


def _generate_ai_text(prompt: str, workflow_name: str, variables: dict[str, str], preferred_model: str = "") -> str:
    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ai_type needs OPENAI_API_KEY in .env.local")
    resolved_prompt = _substitute(prompt, variables).strip()
    if not resolved_prompt:
        raise ValueError("ai_type step has empty ai_prompt")
    prompt_l = resolved_prompt.lower()
    wf_l = workflow_name.lower()
    if "linkedin" in prompt_l or "linkedin" in wf_l:
        resolved_prompt = (
            f"{resolved_prompt}\n\n"
            "Writing rules for output:\n"
            "- Write a polished LinkedIn post in natural human voice.\n"
            "- Use a strong hook, practical takeaway, and 3-5 relevant hashtags.\n"
            "- Do not mention engagement bait, algorithm, or viral.\n"
            "- Output only the final post text."
        )
    model = (preferred_model or os.environ.get("TRAINER_AI_TYPE_MODEL") or "gpt-4o-mini").strip()
    max_tokens = max(32, min(int(os.environ.get("TRAINER_AI_TYPE_MAX_TOKENS") or "600"), 2000))
    max_chars = max(50, min(int(os.environ.get("TRAINER_AI_TYPE_MAX_CHARS") or "2000"), 20000))
    client = OpenAI(api_key=key)
    msg = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "system",
                "content": "You write text for one input field. Return only the final field text.",
            },
            {"role": "user", "content": resolved_prompt},
        ],
    )
    out = (msg.choices[0].message.content or "").strip()
    if not out:
        raise RuntimeError("OpenAI returned empty text for ai_type step")
    return out[:max_chars]


def _media_asset_path(action: dict[str, Any], variables: dict[str, str]) -> str:
    current = variables.get("CURRENT_MEDIA_PATH", "").strip()
    if current:
        return current
    ids = action.get("upload_asset_ids") if isinstance(action.get("upload_asset_ids"), list) else []
    media_index = root() / "media_library" / "index.json"
    if ids and media_index.is_file():
        data = json.loads(media_index.read_text(encoding="utf-8"))
        assets = data.get("assets") if isinstance(data.get("assets"), list) else []
        by_id = {str(a.get("id") or ""): a for a in assets if isinstance(a, dict)}
        for asset_id in ids:
            asset = by_id.get(str(asset_id or ""))
            rel = str((asset or {}).get("relative_path") or "")
            if rel:
                path = root() / rel
                if path.is_file():
                    return str(path.resolve())
    filenames = action.get("upload_filenames") if isinstance(action.get("upload_filenames"), list) else []
    for filename in filenames:
        for base in (root() / "media_library" / "images", root() / "media_library" / "videos"):
            matches = list(base.glob(f"*{filename}")) if base.is_dir() else []
            if matches:
                return str(matches[0].resolve())
    return ""


def _set_clipboard(text: str) -> None:
    import pyperclip  # type: ignore

    pyperclip.copy(text)


def _execute_action(action: dict[str, Any], variables: dict[str, str], run_mode: str) -> str:
    from execution.executor import ExecutionEngine

    executor = ExecutionEngine()
    action_type = str(action.get("action_type") or "click").strip().lower()
    wait_seconds = float(action.get("wait_seconds") or action.get("wait") or 1)
    clear_first = bool(action.get("clear_first", True))

    if action_type in ("open_url", "url", "open_whatsapp"):
        raw_url = "https://web.whatsapp.com/" if action_type == "open_whatsapp" else _substitute(action.get("url"), variables)
        executor.open_url(_ensure_url(raw_url))
        return "open_url"
    if action_type in ("open_chrome", "open_browser"):
        if action.get("url"):
            executor.open_url(_ensure_url(_substitute(action.get("url"), variables)))
        else:
            _open_chrome()
        return "open_browser"
    if action_type in ("close_chrome", "close_browser"):
        _close_chrome()
        return "close_chrome"
    if action_type in ("maximize", "maximise", "maximize_window", "maximise_window"):
        _maximize_window(executor)
        return "maximize"
    if action_type == "minimize":
        if platform.system() == "Darwin":
            executor.shortcut("command", "m")
        else:
            executor.shortcut("win", "down")
        return "minimize"
    if action_type == "wait":
        time.sleep(max(0.0, wait_seconds))
        return "wait"
    if action_type == "press_enter":
        executor.press_enter()
        return "press_enter"
    if action_type == "press_escape":
        executor.press_escape()
        return "press_escape"
    if action_type == "press_space":
        _press_key(executor, "space", int(action.get("tab_count") or action.get("count") or 1))
        return "press_space"
    if action_type == "press_home":
        _press_key(executor, "home", 1)
        time.sleep(0.3)
        return "press_home"
    if action_type == "press_end":
        _press_key(executor, "end", 1)
        time.sleep(0.3)
        return "press_end"
    if action_type in ("press_tab", "tab"):
        if bool(action.get("direct_jump")):
            x = int(action.get("trained_x") or action.get("x") or 0)
            y = int(action.get("trained_y") or action.get("y") or 0)
            if x > 0 and y > 0:
                executor.click(x, y, 1.0)
                return "direct_jump"
        _press_key(executor, "tab", int(action.get("tab_count") or action.get("count") or 1))
        return "press_tab"
    if action_type in ("press_arrow", "press_arrow_left", "press_arrow_right", "press_arrow_up", "press_arrow_down"):
        key = str(action.get("direction") or "").lower()
        if action_type.startswith("press_arrow_"):
            key = action_type.replace("press_arrow_", "")
        if key not in ("left", "right", "up", "down"):
            key = "right"
        _press_key(executor, key, int(action.get("tab_count") or action.get("count") or 1))
        return action_type
    if action_type == "hotkey":
        keys = action.get("hotkey_keys") or action.get("keys") or []
        if isinstance(keys, str):
            keys = [p.strip() for p in keys.replace("+", ",").split(",") if p.strip()]
        executor.shortcut(*list(keys))
        return "hotkey"
    if action_type == "copy":
        executor.copy_selection()
        return "copy"
    if action_type == "paste":
        executor.paste()
        return "paste"
    if action_type == "scroll":
        amount = int(action.get("amount") or action.get("scroll_amount") or 3)
        direction = str(action.get("position_hint") or action.get("direction") or "down").lower()
        if "up" in direction:
            executor.scroll_up(abs(amount))
        else:
            executor.scroll_down(abs(amount))
        return "scroll"
    if action_type in ("type", "type_text", "type_project_name"):
        text = action.get("type_text") if action_type != "type_project_name" else action.get("workflow_name")
        executor.type_text(_substitute(text, variables), clear_first=clear_first)
        return action_type
    if action_type == "type_whatsapp_number":
        text = variables.get("WHATSAPP_NUMBER", "").strip()
        if not text:
            raise ValueError("type_whatsapp_number has no WHATSAPP_NUMBER for this workflow")
        executor.type_text(text, clear_first=clear_first)
        return "type_whatsapp_number"
    if action_type in ("completion_message", "completion_link"):
        text = f"{action.get('description') or 'Automation run complete.'}"
        variables["WHATSAPP_COMPLETION_TEXT"] = _substitute(text, variables)
        return action_type
    if action_type == "type_completion_message":
        text = variables.get("WHATSAPP_COMPLETION_TEXT", "").strip()
        if not text:
            text = str(action.get("type_text") or "Automation run complete.").strip()
        executor.type_text(_substitute(text, variables), clear_first=False)
        variables.pop("WHATSAPP_COMPLETION_TEXT", None)
        return "type_completion_message"
    if action_type == "ai_type":
        text = _generate_ai_text(
            str(action.get("ai_prompt") or ""),
            str(variables.get("WORKFLOW_NAME") or ""),
            variables,
            str(action.get("ai_model") or ""),
        )
        executor.type_text(text, clear_first=clear_first)
        variables["LAST_TYPED_TEXT"] = text
        return "ai_type"
    if action_type == "upload":
        layer_u = str(action.get("calendar_upload_layer") or "").strip().lower()
        pick_u = str(action.get("calendar_asset_pick") or "auto").strip().lower()
        if pick_u not in ("auto", "image", "video", "text"):
            pick_u = "auto"
        if layer_u in ("core", "hybrid", "ai"):
            from cusear.media_folders import select_calendar_asset_for_upload

            media_path_cal, media_kind_cal, caption_u, cap_path_u = select_calendar_asset_for_upload(
                variables,
                layer=layer_u,
                pick=pick_u,
            )
            cap_st = (caption_u or "").strip()
            prev_cap = str(variables.get("CURRENT_CAPTION", "")).strip()
            merged_cap = cap_st if cap_st else prev_cap
            variables["CURRENT_CAPTION"] = merged_cap
            if cap_path_u.strip():
                variables["CURRENT_CAPTION_PATH"] = cap_path_u.strip()
            variables["CURRENT_IMAGE_PATH"] = media_path_cal.strip() if media_kind_cal == "image" else ""
            variables["CURRENT_VIDEO_PATH"] = media_path_cal.strip() if media_kind_cal == "video" else ""
            variables["CURRENT_MEDIA_KIND"] = media_kind_cal or ""
            if media_path_cal.strip():
                variables["CURRENT_MEDIA_PATH"] = media_path_cal.strip()
                _set_clipboard(media_path_cal.strip())
                return "upload"
            if media_kind_cal == "text" and merged_cap:
                variables["CURRENT_MEDIA_PATH"] = ""
                _set_clipboard(merged_cap)
                return "upload"
            raise FileNotFoundError(
                f"calendar upload: missing {layer_u} assets for "
                f"day {variables.get('CURRENT_CALENDAR_DAY', '?')} (pick={pick_u})"
            )
        media_path = _media_asset_path(action, variables)
        if not media_path:
            raise FileNotFoundError("upload step could not resolve a media file")
        variables["CURRENT_MEDIA_PATH"] = media_path
        _set_clipboard(media_path)
        return "upload"
    if action_type in ("click", "double_click", "right_click"):
        if run_mode == "smart" or action.get("live_vision"):
            try:
                from execution.fallback import execute_with_fallback
                from vision import vision_engine as ve

                intent = str(action.get("intent") or action.get("description") or "").strip()
                labels = action.get("labels") or []
                pos_hint = str(action.get("position_hint") or "anywhere")
                result = execute_with_fallback(
                    platform="any",
                    action_key=intent or "target",
                    config={"platforms": {"any": {"actions": {intent or "target": {"labels": labels, "position_hint": pos_hint}}}}},
                    vision_engine=ve,
                    executor=executor,
                )
                if getattr(result, "success", False):
                    return f"smart_click:{getattr(result, 'method', 'fallback')}"
            except Exception:
                if not action.get("x") and not action.get("y"):
                    raise
        x = int(action.get("x") or action.get("click_x") or 0)
        y = int(action.get("y") or action.get("click_y") or 0)
        confidence = float(action.get("confidence") or 1.0)
        if action_type == "double_click":
            executor.double_click(x, y, confidence)
        elif action_type == "right_click":
            executor.right_click(x, y, confidence)
        else:
            executor.click(x, y, confidence)
        return action_type
    raise ValueError(f"Unsupported action_type: {action_type}")


def run_workflow(
    workflow_name: str,
    *,
    dry_run: bool = False,
    variables: dict[str, str] | None = None,
    run_mode: str = "smart",
    stop_event: Event | None = None,
    status_cb: StatusCallback | None = None,
    bundle_slug: str = "",
) -> WorkflowRunResult:
    variables = dict(variables or {})
    variables.setdefault("CURRENT_WORKFLOW_KEY", str(workflow_name or "").strip())
    variables.setdefault("CURRENT_WORKFLOW_NAME", str(variables.get("WORKFLOW_NAME") or workflow_name or "").strip())
    variables.setdefault(
        "CURRENT_AR_FLOW_NAME",
        str(
            variables.get("CURRENT_AR_FLOW_NAME")
            or variables.get("CURRENT_AR_BUNDLE_NAME")
            or variables.get("CURRENT_AR_BUNDLE_SLUG")
            or bundle_slug
            or workflow_name
            or ""
        ).strip(),
    )
    variables.setdefault("CALENDAR_AI_VARIANT", str(os.environ.get("CUSEAR_DEFAULT_AI_VARIANT") or "budget").strip())
    variables.setdefault("CALENDAR_AI_PLATFORM", str(os.environ.get("CUSEAR_DEFAULT_AI_PLATFORM") or "instagram").strip())
    try:
        from cusear.media_folders import apply_calendar_runtime_tokens, downloads_dir as _cdd

        apply_calendar_runtime_tokens(
            variables,
            downloads_base=_cdd(),
            workflow_stem=str(workflow_name or "").strip(),
            workflow_label=str(variables.get("CURRENT_WORKFLOW_NAME") or "").strip(),
        )
    except Exception:
        pass
    path = workflow_path(workflow_name)
    workflow = load_json(path)
    steps = workflow.get("steps") or []
    step_results: list[dict[str, Any]] = []
    error = ""
    _emit(status_cb, f"Running {workflow_name} ({len(steps)} steps)")

    for index, step in enumerate(steps, start=1):
        if stop_event and stop_event.is_set():
            error = "Stopped by user."
            break
        action = _action_for_step(step)
        started = _dt.datetime.utcnow().isoformat() + "Z"
        desc = str(action.get("description") or action.get("instruction") or action.get("action_type") or f"step {index}")
        result = {
            "step": action.get("step") or index,
            "description": desc,
            "started_at": started,
            "status": "ok",
            "method": "dry_run" if dry_run else "",
            "error": "",
        }
        try:
            _emit(status_cb, f"{workflow_name}: step {index}/{len(steps)} - {desc}")
            if not dry_run:
                result["method"] = _execute_action(action, variables, run_mode)
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            error = result["error"]
        finally:
            result["finished_at"] = _dt.datetime.utcnow().isoformat() + "Z"
            step_results.append(result)
        if error:
            break

    audit = save_run_audit(
        workflow_name=workflow_name,
        dry_run=dry_run,
        steps=step_results,
        error=error,
        bundle_slug=bundle_slug,
    )
    ok = not error
    _emit(status_cb, f"{workflow_name}: {'ok' if ok else 'failed'} ({audit})")
    return WorkflowRunResult(name=workflow_name, ok=ok, audit_path=str(audit), error=error)


def run_bundle(
    bundle_slug: str | None = None,
    *,
    dry_run: bool = False,
    variables: dict[str, str] | None = None,
    run_mode: str = "smart",
    stop_event: Event | None = None,
    status_cb: StatusCallback | None = None,
) -> list[WorkflowRunResult]:
    slug, bundle = load_bundle(bundle_slug)
    children = [str(x or "").strip() for x in (bundle.get("children") or []) if str(x or "").strip()]
    if not children:
        raise ValueError(f"Bundle {slug} has no child workflows.")
    results: list[WorkflowRunResult] = []
    for child in children:
        if stop_event and stop_event.is_set():
            break
        child_vars = dict(variables or {})
        child_vars["WORKFLOW_NAME"] = child
        number = ""
        numbers = bundle.get("notify_numbers_by_flow") if isinstance(bundle.get("notify_numbers_by_flow"), dict) else {}
        if numbers:
            number = str(numbers.get(child) or "")
        if not number:
            number = str(bundle.get("notify_number") or "")
        if number:
            child_vars["WHATSAPP_NUMBER"] = number
        result = run_workflow(
            child,
            dry_run=dry_run,
            variables=child_vars,
            run_mode=run_mode,
            stop_event=stop_event,
            status_cb=status_cb,
            bundle_slug=slug,
        )
        results.append(result)
        if number:
            state = "completed" if result.ok else f"failed: {result.error}"
            send_whatsapp_confirmation(number, f"{child} {state}. Log: {result.audit_path}")
        if not result.ok:
            break
    return results
