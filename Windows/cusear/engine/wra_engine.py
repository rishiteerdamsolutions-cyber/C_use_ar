from __future__ import annotations

import argparse
import os
import threading
import time
from typing import Any

from .agami import Agami
from .aha import AHA
from .focus_mode import FocusMode
from .logging_utils import setup_basic_logging, ts_compact, write_json
from .lucky import Lucky
from .mouse_guard import MouseGuard
from .paths import WraPaths, ensure_dirs
from .preflight import (
    check_chrome,
    check_content_files,
    check_disk_space,
    check_internet,
    run_preflight,
)
from .session_steps import SessionSteps
from .shared_state import SharedState
from .workflow import clone_steps, expand_runtime_navigation_steps, load_workflow, renumber_steps, save_workflow


def _session_clone_path(paths: WraPaths, workflow_name: str) -> str:
    return os.path.join(paths.sessions_dir, f"{workflow_name}_{ts_compact()}_session.json")


def run_wra(
    *,
    repo_root: str,
    workflow_path: str,
    content_map: dict[str, Any],
    company_endpoint: str | None = None,
    enable_mouse_guard: bool = True,
    enable_focus_mode: bool = True,
) -> dict[str, Any]:
    """
    Orchestrator: Lucky -> Agami -> AHA™.
    """

    paths = WraPaths(root=repo_root)
    ensure_dirs(
        paths.sessions_dir,
        paths.lucky_logs_dir,
        paths.agami_logs_dir,
        paths.screenshots_dir,
        paths.company_logs_dir,
        paths.preflight_logs_dir,
    )

    workflow = load_workflow(workflow_path)
    workflow_name = str(workflow.get("workflow_name") or os.path.basename(workflow_path).replace(".json", ""))

    # Preflight
    content_files = [p for p in content_map.values() if isinstance(p, str) and os.path.exists(p)]
    pf = run_preflight(
        [
            ("internet", lambda: check_internet()),
            ("chrome", lambda: check_chrome()),
            ("disk_space", lambda: check_disk_space()),
            ("content_files", lambda: check_content_files(content_files)),
        ]
    )
    preflight_path = os.path.join(paths.preflight_logs_dir, f"preflight_{ts_compact()}.json")
    write_json(preflight_path, {"ok": pf.ok, "failed_check": pf.failed_check, "details": pf.details})
    if not pf.ok:
        return {"ok": False, "stage": "preflight", "reason": pf.failed_check, "details": pf.details}

    focus = FocusMode()
    mouse_guard = MouseGuard()

    if enable_focus_mode:
        focus.enable()
    if enable_mouse_guard:
        mouse_guard.start()

    try:
        # Lucky dry run
        lucky = Lucky(logs_dir=paths.lucky_logs_dir, company_endpoint=company_endpoint)
        lucky_report = lucky.run(workflow)
        if lucky_report.signal != "GREEN":
            return {"ok": False, "stage": "lucky", "report": lucky_report.to_dict()}

        # Create session clone (original never touched)
        session_workflow = dict(workflow)
        session_steps = clone_steps(list(session_workflow.get("steps") or []))
        renumber_steps(session_steps, start_at=1)
        expand_runtime_navigation_steps(session_steps)
        session_workflow["steps"] = session_steps
        session_workflow["session_created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        session_workflow["session_source"] = os.path.abspath(workflow_path)

        shared = SharedState()
        session = SessionSteps(session_steps)
        agami = Agami(
            company_endpoint=company_endpoint,
            company_logs_dir=paths.company_logs_dir,
            screenshots_dir=paths.screenshots_dir,
        )
        aha = AHA(
            company_endpoint=company_endpoint,
            company_logs_dir=paths.company_logs_dir,
            screenshots_dir=paths.screenshots_dir,
        )

        agami_thread = threading.Thread(target=agami.walk, args=(session, shared), daemon=True)
        aha_thread = threading.Thread(target=aha.execute, args=(session, content_map, shared), daemon=True)

        agami_thread.start()
        aha_thread.start()

        aha_thread.join()
        agami_thread.join(timeout=2.0)

        # Save session clone to logs/sessions
        session_path = _session_clone_path(paths, workflow_name)
        save_workflow(session_path, session_workflow)

        if shared.abort:
            return {
                "ok": False,
                "stage": "runtime",
                "reason": shared.abort_reason,
                "session_path": session_path,
                "lucky_report": lucky_report.to_dict(),
            }

        return {
            "ok": True,
            "stage": "complete",
            "session_path": session_path,
            "lucky_report": lucky_report.to_dict(),
        }

    finally:
        if enable_mouse_guard:
            mouse_guard.stop()
        if enable_focus_mode:
            focus.disable()


def _parse_kv_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> None:
    setup_basic_logging()

    parser = argparse.ArgumentParser(description="cusear™ WRA Engine v2 runner")
    parser.add_argument("--repo-root", default=os.getcwd())
    parser.add_argument("--workflow", required=True, help="Path to WRA v2 workflow JSON")
    parser.add_argument(
        "--content",
        action="append",
        default=[],
        help="content_key=value pairs; values can be literal text or a file path",
    )
    parser.add_argument("--company-endpoint", default=os.environ.get("COMPANY_ENDPOINT", ""))
    args = parser.parse_args()

    content_map = _parse_kv_pairs(args.content)

    # If value is an existing file, load file content.
    resolved: dict[str, Any] = {}
    for k, v in content_map.items():
        if os.path.exists(v) and os.path.isfile(v):
            with open(v, "r", encoding="utf-8") as f:
                resolved[k] = f.read()
        else:
            resolved[k] = v

    result = run_wra(
        repo_root=args.repo_root,
        workflow_path=args.workflow,
        content_map=resolved,
        company_endpoint=(args.company_endpoint or None),
    )
    print(result)


if __name__ == "__main__":
    main()

