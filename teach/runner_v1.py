"""
Runner V1 — Training Only Mode
Autonomous Web Agency Agent v1.0

Replays a workflow using ONLY the coordinates and actions recorded during
training. Zero AI API calls at runtime — fast, offline-capable, free.

Best for:
  ✓ Workflows where the UI never changes (same screen layout every time)
  ✓ Saving API costs on high-volume repetitive tasks
  ✓ Running without internet (after training is done)
  ✓ Maximum speed

Limitation:
  ✗ If a UI element moves or the page layout changes, it will click the
    wrong place. Use Runner V2 (AI mode) for resilience.

Usage:
    from teach.runner_v1 import RunnerV1
    runner = RunnerV1("deploy_vercel")
    runner.run()

Or via CLI:
    python main.py --run-workflow deploy_vercel --mode fast
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent.parent
WORKFLOWS_DIR = BASE_DIR / "workflows"


class RunnerV1:
    """
    Training-Only workflow runner.

    Uses the x,y coordinates saved during the --teach session.
    No vision API calls. Executes instantly.

    Args:
        workflow_name: Matches workflows/<name>.json
        dry_run:       Print steps without executing GUI actions.
        speed:         Pause between steps in seconds (default 0.5).
    """

    MODE = "training_only"

    def __init__(
        self,
        workflow_name: str,
        dry_run: bool = False,
        speed: float = 0.5,
    ) -> None:
        self.workflow_name = workflow_name
        self.dry_run = dry_run
        self.speed = speed
        self._workflow = self._load(workflow_name)

        if not dry_run:
            from execution.executor import ExecutionEngine
            self._executor = ExecutionEngine()
        else:
            self._executor = None

        logger.info(
            "RunnerV1 (training-only) loaded '%s'  steps=%d  dry_run=%s",
            workflow_name,
            self._workflow.get("total_steps", 0),
            dry_run,
        )

    def total_steps(self) -> int:
        return int(self._workflow.get("total_steps", len(self._workflow.get("steps", []))))

    # ── Load ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _load(name: str) -> dict[str, Any]:
        """
        Load a workflow by name.
        Production: loads encrypted .enc file (machine-ID-bound).
        Development: falls back to plain .json (only if APP_MODE=development).
        """
        import os
        from security.workflow_crypto import load_workflow

        # Try encrypted first (.enc)
        enc_path  = WORKFLOWS_DIR / f"{name}.enc"
        json_path = WORKFLOWS_DIR / f"{name}.json"

        if enc_path.exists():
            # Get machine ID from active license
            try:
                from security.license import get_machine_id
                machine_id = get_machine_id()
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot load workflow '{name}': license check failed — {exc}"
                )
            return load_workflow(enc_path, machine_id)

        elif json_path.exists():
            # Plain JSON — only in dev mode
            return load_workflow(json_path, machine_id="")

        else:
            enc_available  = [p.stem for p in WORKFLOWS_DIR.glob("*.enc")]
            json_available = [p.stem for p in WORKFLOWS_DIR.glob("*.json")]
            available = enc_available or json_available
            raise FileNotFoundError(
                f"Workflow '{name}' not found.\n"
                f"Available: {available or ['none — run --teach first']}"
            )

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self, variables: dict[str, str] | None = None) -> bool:
        """
        Execute all steps using saved training coordinates.

        Args:
            variables: {placeholder: value} substitutions for type_text.
                       E.g. {"{{SITE_NAME}}": "priya-salon"}

        Returns:
            True if all steps completed without error.
        """
        from analytics.session import SessionRecorder

        steps: list[dict] = self._workflow.get("steps", [])
        total  = len(steps)
        variables = variables or {}

        recorder = SessionRecorder(
            user_email="agent@local",
            workflow_name=f"{self.workflow_name}__v1",
        )

        print(f"\n{'═'*60}")
        print(f"  ▶  [V1 Training-Only]  '{self.workflow_name}'  ({total} steps)")
        print(f"  ⚡  Zero API calls — using saved training coordinates")
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
                self._print_dry(act_type, action, variables)
                recorder.log_step(f"step_{step_num}", "SKIP", 0, "dry_run")
                continue

            t0 = time.time()
            success, method, error = self._execute(action, variables)
            duration = time.time() - t0

            status = "SUCCESS" if success else "FAILURE"
            recorder.log_step(
                step_name=f"step_{step_num}_{act_type}",
                status=status,
                duration_seconds=duration,
                method=method,
                error=error,
            )

            if success:
                print(f"    ✓  {method}  ({duration:.2f}s)\n")
            else:
                print(f"    ✗  FAILED — {error}\n")
                all_ok = False

            time.sleep(self.speed)

        session_path = recorder.save_session()
        rate = recorder.calculate_success_rate()

        print(f"{'═'*60}")
        print(f"  Done  |  success={rate:.0%}  |  mode=training-only  |  log={session_path.name}")
        print(f"{'═'*60}\n")

        return all_ok

    # ── Execute one action ────────────────────────────────────────────────────
    def _execute(
        self,
        action: dict[str, Any],
        variables: dict[str, str],
    ) -> tuple[bool, str, str]:
        """
        Execute action using saved training data.
        Returns (success, method, error_message).
        """
        act_type    = action.get("action_type", "click")
        trained_x   = action.get("trained_x", 0)
        trained_y   = action.get("trained_y", 0)
        confidence  = float(action.get("confidence", 0.9))
        type_text   = self._sub(action.get("type_text", ""), variables)
        hotkey_keys = action.get("hotkey_keys", [])
        url         = self._sub(action.get("url", ""), variables)
        wait_sec    = float(action.get("wait_seconds", 1))
        clear_first = bool(action.get("clear_first", True))

        try:
            if act_type == "open_url" and url:
                self._executor.open_url(url)
                return True, "open_url", ""

            elif act_type == "wait":
                time.sleep(wait_sec)
                return True, "wait", ""

            elif act_type == "hotkey" and hotkey_keys:
                self._executor.shortcut(*hotkey_keys)
                return True, "hotkey", ""

            elif act_type == "copy":
                self._executor.copy_selection()
                return True, "copy", ""

            elif act_type == "paste":
                self._executor.paste()
                return True, "paste", ""

            elif act_type == "scroll":
                hint = action.get("position_hint", "down").lower()
                if "up" in hint:
                    self._executor.scroll_up(3)
                else:
                    self._executor.scroll_down(3)
                return True, "scroll", ""

            elif act_type == "type":
                if not type_text:
                    return False, "type", "type_text is empty"
                self._executor.type_text(type_text, clear_first=clear_first)
                return True, "type", ""

            elif act_type == "click":
                if not trained_x and not trained_y:
                    return False, "click_trained", "No trained_x/y — re-run --teach to capture coordinates"
                # Use saved coordinates directly — no API call
                self._executor.click(trained_x, trained_y, confidence)
                return True, f"click_trained({trained_x},{trained_y})", ""

            else:
                return False, "unknown", f"Unknown action_type: {act_type}"

        except Exception as exc:
            logger.error("V1 action failed: %s", exc)
            return False, "exception", str(exc)

    @staticmethod
    def _sub(text: str, variables: dict[str, str]) -> str:
        for k, v in variables.items():
            text = text.replace(k, v)
        return text

    @staticmethod
    def _print_dry(act_type: str, action: dict, variables: dict) -> None:
        x = action.get("trained_x", "?")
        y = action.get("trained_y", "?")
        text = action.get("type_text", "")
        for k, v in variables.items():
            text = text.replace(k, v)
        details = {
            "click":    f"→ trained coords ({x}, {y})",
            "type":     f"→ '{text}'",
            "hotkey":   f"→ {action.get('hotkey_keys',[])}",
            "open_url": f"→ {action.get('url','')}",
            "scroll":   f"→ {action.get('position_hint','down')}",
            "wait":     f"→ {action.get('wait_seconds',1)}s",
            "copy":     "→ ⌘C / Ctrl+C",
            "paste":    "→ ⌘V / Ctrl+V",
        }.get(act_type, "→ ?")
        print(f"    [DRY RUN]  {act_type}  {details}\n")
