"""
Workflow Runner — Autonomous Web Agency Agent v1.0

Replays a saved workflow JSON step by step.
For every step it:
  1. Takes a fresh live screenshot
  2. Uses Claude Vision to find the target element on the CURRENT screen
     (handles UI changes — never uses hardcoded coordinates)
  3. Executes the recorded action
  4. Falls back through the 4-attempt cascade if needed
  5. Logs every step to a session file

Usage:
    from teach.workflow_runner import WorkflowRunner

    runner = WorkflowRunner("deploy_vercel")
    runner.run()
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
WORKFLOWS_DIR = BASE_DIR / "workflows"


class WorkflowRunner:
    """
    Replays a taught workflow from its JSON definition.

    Args:
        workflow_name: Name of the workflow (matches workflows/<name>.json).
        dry_run:       If True, prints steps but doesn't execute GUI actions.
    """

    def __init__(self, workflow_name: str, dry_run: bool = False) -> None:
        self.workflow_name = workflow_name
        self.dry_run = dry_run
        self._workflow: dict[str, Any] = self._load(workflow_name)

        if not dry_run:
            from execution.executor import ExecutionEngine
            self._executor = ExecutionEngine()
        else:
            self._executor = None

        logger.info(
            "WorkflowRunner loaded '%s'  steps=%d  dry_run=%s",
            workflow_name,
            self._workflow.get("total_steps", 0),
            dry_run,
        )

    # ── Load ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _load(name: str) -> dict[str, Any]:
        path = WORKFLOWS_DIR / f"{name}.json"
        if not path.exists():
            available = [p.stem for p in WORKFLOWS_DIR.glob("*.json")]
            raise FileNotFoundError(
                f"Workflow '{name}' not found.\n"
                f"Available workflows: {available or ['none yet — run --teach first']}"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self, variables: dict[str, str] | None = None) -> bool:
        """
        Execute all steps in the workflow.

        Args:
            variables: Optional dict of {placeholder: value} substitutions.
                       E.g. {"{{CLIENT_NAME}}": "Priya Salon"} will replace
                       that placeholder in any type_text values.

        Returns:
            True if all steps succeeded (or recovered), False if any failed hard.
        """
        from analytics.session import SessionRecorder
        from execution.fallback import execute_with_fallback

        steps: list[dict] = self._workflow.get("steps", [])
        total = len(steps)
        variables = variables or {}

        recorder = SessionRecorder(
            user_email="agent@local",
            workflow_name=self.workflow_name,
        )

        print(f"\n{'═'*60}")
        print(f"  ▶  Running workflow: '{self.workflow_name}'  ({total} steps)")
        if self.dry_run:
            print("  [DRY RUN — no GUI actions will be executed]")
        print(f"{'═'*60}\n")

        all_ok = True

        for step_def in steps:
            step_num  = step_def.get("step", "?")
            instr     = step_def.get("instruction", "")
            action    = step_def.get("action", {})
            act_type  = action.get("action_type", "click")

            print(f"  Step {step_num}/{total}: {instr}")

            if self.dry_run:
                print(f"    [DRY RUN] Would execute: {act_type}  →  {action.get('intent','')}\n")
                recorder.log_step(f"step_{step_num}", "SKIP", 0, "dry_run")
                continue

            t0 = time.time()
            success, method = self._execute_action(action, variables)
            duration = time.time() - t0

            status = "SUCCESS" if success else "FAILURE"
            recorder.log_step(
                step_name=f"step_{step_num}_{act_type}",
                status=status,
                duration_seconds=duration,
                method=method,
                error="" if success else f"Action failed: {act_type}",
            )

            if success:
                print(f"    ✓ Done  ({method}, {duration:.1f}s)\n")
            else:
                print(f"    ✗ FAILED  ({method}, {duration:.1f}s)\n")
                all_ok = False

            # Small pause between steps for stability
            time.sleep(0.8)

        session_path = recorder.save_session()
        rate = recorder.calculate_success_rate()

        print(f"{'═'*60}")
        print(f"  Workflow complete  |  success_rate={rate:.0%}  |  log={session_path.name}")
        print(f"{'═'*60}\n")

        return all_ok

    # ── Execute a single action ───────────────────────────────────────────────
    def _execute_action(
        self,
        action: dict[str, Any],
        variables: dict[str, str],
    ) -> tuple[bool, str]:
        """
        Execute one action dict. Returns (success, method_used).
        """
        from vision import vision_engine as ve
        from execution.executor import LowConfidenceError

        act_type     = action.get("action_type", "click")
        intent       = action.get("intent", "")
        labels       = action.get("labels", [])
        pos_hint     = action.get("position_hint", "anywhere")
        type_text    = self._substitute(action.get("type_text", ""), variables)
        hotkey_keys  = action.get("hotkey_keys", [])
        url          = self._substitute(action.get("url", ""), variables)
        wait_sec     = float(action.get("wait_seconds", 1))
        clear_first  = bool(action.get("clear_first", True))

        try:
            # ── open_url ───────────────────────────────────────────────────────
            if act_type == "open_url" and url:
                self._executor.open_url(url)
                return True, "open_url"

            # ── wait ──────────────────────────────────────────────────────────
            elif act_type == "wait":
                logger.info("Waiting %.1f seconds", wait_sec)
                time.sleep(wait_sec)
                return True, "wait"

            # ── hotkey ────────────────────────────────────────────────────────
            elif act_type == "hotkey" and hotkey_keys:
                self._executor.shortcut(*hotkey_keys)
                return True, "hotkey"

            # ── copy / paste (selection / clipboard) ────────────────────────────
            elif act_type == "copy":
                self._executor.copy_selection()
                return True, "copy"

            elif act_type == "paste":
                self._executor.paste()
                return True, "paste"

            # ── scroll ────────────────────────────────────────────────────────
            elif act_type == "scroll":
                if "up" in pos_hint.lower():
                    self._executor.scroll_up(3)
                else:
                    self._executor.scroll_down(3)
                return True, "scroll"

            # ── type ──────────────────────────────────────────────────────────
            elif act_type == "type":
                if type_text:
                    self._executor.type_text(type_text, clear_first=clear_first)
                    return True, "type"
                return False, "type_empty"

            # ── click (primary action) ────────────────────────────────────────
            elif act_type == "click":
                screenshot_b64 = ve.take_screenshot()
                result = ve.find_element(
                    intent=intent,
                    labels=labels,
                    position_hint=pos_hint,
                    screenshot_b64=screenshot_b64,
                )
                if result.get("found"):
                    self._executor.click(result["x"], result["y"], result["confidence"])
                    return True, f"vision_{result.get('provider','?')}"

                # Vision said not found — try fallback cascade
                logger.warning("Vision could not find element — trying fallback")
                fb = _fallback_click(intent, labels, pos_hint, self._executor)
                return fb.success, fb.method

            else:
                logger.warning("Unknown action_type: %s", act_type)
                return False, "unknown_action"

        except LowConfidenceError as e:
            logger.warning("Low confidence on step: %s", e)
            # Try fallback
            fb = _fallback_click(intent, labels, pos_hint, self._executor)
            return fb.success, fb.method

        except Exception as e:
            logger.error("Action execution error: %s", e, exc_info=True)
            return False, f"exception: {type(e).__name__}"

    @staticmethod
    def _substitute(text: str, variables: dict[str, str]) -> str:
        """Replace {{PLACEHOLDER}} tokens with actual values."""
        for key, val in variables.items():
            text = text.replace(key, val)
        return text


# ─── Fallback helper ──────────────────────────────────────────────────────────
def _fallback_click(intent, labels, pos_hint, executor):
    """Attempt a vision-guided click with the fallback cascade."""
    from execution.fallback import execute_with_fallback, FallbackResult
    from vision import vision_engine as ve

    class _SimpleConfig:
        """Minimal config shim for fallback manager."""
        def get(self, *_): return {}

    return execute_with_fallback(
        platform="any",
        action_key=intent,
        config={"platforms": {"any": {"actions": {
            intent: {"labels": labels, "position_hint": pos_hint}
        }}}},
        vision_engine=ve,
        executor=executor,
    )


# ─── List available workflows ─────────────────────────────────────────────────
def list_workflows() -> list[str]:
    """Return names of all saved workflows."""
    if not WORKFLOWS_DIR.exists():
        return []
    return [p.stem for p in sorted(WORKFLOWS_DIR.glob("*.json"))]


def print_workflow_summary(name: str) -> None:
    """Pretty-print a workflow's steps."""
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        print(f"Workflow '{name}' not found.")
        return
    wf = json.loads(path.read_text())
    print(f"\n  Workflow: {wf['workflow_name']}  ({wf['total_steps']} steps)")
    print(f"  Taught:  {wf.get('taught_at','?')}\n")
    for s in wf["steps"]:
        act = s["action"]
        print(
            f"    {s['step']:2d}. [{act['action_type']:<8}]  {s['instruction']}"
        )
    print()
