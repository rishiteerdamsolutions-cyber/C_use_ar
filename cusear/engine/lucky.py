from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
from urllib.parse import urlparse
from typing import Any

from .constants import (
    ESCAPE_WAIT,
    LUCKY_ARROW_WAIT,
    LUCKY_BETWEEN_STEPS,
    LUCKY_CAPTURE_WAIT,
    LUCKY_ENTER_WAIT,
    LUCKY_REFRESH_WAIT,
    LUCKY_TAB_CAPTURE_MODE,
    LUCKY_TAB_INTERVAL,
    LUCKY_TYPE_INTERVAL,
    LUCKY_URL_LOAD_WAIT,
)
from .logging_utils import ts_compact, write_json
from .os_adapter import get_os_adapter, open_url_in_google_chrome
from .workflow import (
    LuckyReport,
    WorkflowJson,
    WorkflowStep,
    anchor_has_validation_signal,
    elements_match,
    nearest_checkpoint_index,
)

logger = logging.getLogger(__name__)


class Lucky:
    """
    Dry-run validator. Runs before every real run in the SAME Chrome session.
    """

    def __init__(
        self,
        *,
        logs_dir: str,
        company_endpoint: str | None = None,
        stop_event: Any = None,
    ) -> None:
        self._os = get_os_adapter()
        self._logs_dir = logs_dir
        self._company_endpoint = company_endpoint
        self._stop_event = stop_event
        self._executed_step_actions = 0
        self._refresh_on_mismatch = str(os.environ.get("LUCKY_REFRESH_ON_MISMATCH", "0")).strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._action_logger = self._setup_action_logger()

    def _setup_action_logger(self) -> logging.Logger:
        os.makedirs(self._logs_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(self._logs_dir, f"lucky_{stamp}.log")
        name = f"cusear.lucky.actions.{stamp}.{id(self)}"
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d [LUCKY] ACTION=%(message)s",
                datefmt="%H:%M:%S",
            )
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            lg.addHandler(fh)
            lg.addHandler(sh)
        return lg

    def _alog(self, action: str, result: str) -> None:
        self._action_logger.info("%s RESULT=%s", action, result)

    def _primary_mod(self) -> str:
        import platform

        return "command" if platform.system() == "Darwin" else "ctrl"

    def _activate_chrome(self) -> None:
        self._os.safe_activate_chrome()

    def _maximise(self) -> None:
        import platform as _plat
        import pyautogui  # type: ignore

        if _plat.system() == "Darwin":
            # Avoid command/control/f on macOS to prevent opening browser Find box.
            return
        else:
            pyautogui.hotkey("win", "up")

    def _wait_secs(self, step: WorkflowStep) -> float:
        if step.get("wait_seconds") is not None:
            return float(step["wait_seconds"])
        return float(step.get("duration", 1.0))

    def _trainer_launch_chrome(self) -> None:
        """Match Trainer / Rekky enrich — Lucky dry-run must load Chrome before Tab validation."""
        import platform as plat

        sys_name = plat.system()
        if sys_name == "Darwin":
            subprocess.Popen(["open", "-a", "Google Chrome"])
        elif sys_name == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
        else:
            subprocess.Popen(["google-chrome"])
        time.sleep(1.5)

    def _trainer_type_omnibar(self, step: WorkflowStep) -> None:
        import pyautogui  # type: ignore

        txt = str(step.get("type_text") or "").strip()
        if not txt and str(step.get("description") or "").strip().lower().startswith("http"):
            txt = str(step.get("description") or "").strip()
        low = txt.lower()
        if low.startswith("http://") or low.startswith("https://"):
            self._open_url_like({"url": txt, "step": step.get("step")})
            return

        mod = self._primary_mod()
        self._os.safe_activate_chrome()
        time.sleep(0.35)
        pyautogui.hotkey(mod, "l")
        time.sleep(0.28)
        if txt:
            pyautogui.hotkey(mod, "a")
            time.sleep(0.06)
            pyautogui.press("backspace")
            pyautogui.write(txt, interval=0.015)
        time.sleep(LUCKY_CAPTURE_WAIT)

    def _trainer_close_chrome(self) -> None:
        import platform as plat

        try:
            if plat.system() == "Darwin":
                subprocess.run(
                    ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                    timeout=15,
                    capture_output=True,
                )
            elif plat.system() == "Windows":
                subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], timeout=8, capture_output=True)
        except Exception:
            pass
        time.sleep(0.8)

    def _open_url_like(self, step: WorkflowStep) -> None:
        import pyautogui  # type: ignore

        target_url = str(step.get("url") or "")
        self._alog(f"OPEN_URL_ATTEMPT url={target_url}", "started")
        open_url_in_google_chrome(target_url)
        time.sleep(LUCKY_URL_LOAD_WAIT)
        self._activate_chrome()
        pyautogui.press("escape", presses=2)
        self._maximise()
        time.sleep(0.5)
        pyautogui.press("escape", presses=2)
        self._alog(f"OPEN_URL_ATTEMPT url={target_url}", f"page_load_waited_{LUCKY_URL_LOAD_WAIT}s")

    def _active_tab_title(self) -> str:
        if platform.system() != "Darwin":
            return ""
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Google Chrome" to get title of active tab of front window',
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return str(result.stdout or "").strip()
        except Exception:
            return ""

    def _expected_title_token(self, step: WorkflowStep) -> str:
        raw_url = str(step.get("url") or "").strip().lower()
        host = ""
        if raw_url:
            try:
                parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
                host = str(parsed.netloc or parsed.path or "").lower()
            except Exception:
                host = raw_url
        if "instagram" in host:
            return "instagram"
        if "facebook" in host:
            return "facebook"
        if "linkedin" in host:
            return "linkedin"
        if "twitter" in host or host.endswith("x.com") or "/x.com" in host:
            return "x"
        if "whatsapp" in host:
            return "whatsapp"
        return ""

    def _verify_page_loaded(self, step: WorkflowStep, timeout_s: float = 10.0) -> tuple[bool, str]:
        token = self._expected_title_token(step)
        if not token:
            return True, ""
        deadline = time.time() + max(1.0, timeout_s)
        last_title = ""
        while time.time() < deadline:
            title = self._active_tab_title()
            last_title = title
            if token in title.lower():
                self._alog(f"PAGE_VERIFY token={token} title={title}", "loaded")
                return True, title
            time.sleep(0.4)
        self._alog(f"PAGE_VERIFY token={token} last_title={last_title}", "timeout")
        logger.error("Lucky: Page did not load for token=%s, last_title=%r", token, last_title)
        return False, last_title

    def _exec_step_spacing(self, step: WorkflowStep) -> None:
        wait = float(step.get("wait", 0.5) or 0.0)
        time.sleep(wait)
        time.sleep(LUCKY_BETWEEN_STEPS)
        self._executed_step_actions += 1

    def _simulate_step_dense(self, step: WorkflowStep) -> None:
        """Execute workflow step timings without Lucky between-step spacer."""
        import pyautogui  # type: ignore

        action = step.get("action_type")
        wait = float(step.get("wait", 0.5) or 0.0)

        if action == "open_url":
            self._open_url_like(step)
        elif action == "press_tab":
            pyautogui.press("tab", presses=int(step["tab_count"]), interval=LUCKY_TAB_INTERVAL)
        elif action == "press_enter":
            if step.get("is_final") or step.get("is_destination"):
                logger.info("Lucky: skipping destination Enter (dry run)")
                time.sleep(wait)
                return
            pyautogui.press("enter")
        elif action == "press_space":
            if step.get("is_final") or step.get("is_destination"):
                logger.info("Lucky: skipping destination Space (dry run)")
                time.sleep(wait)
                return
            pyautogui.press("space", presses=int(step.get("count", 1)))
        elif action == "press_escape":
            pyautogui.press("escape", presses=int(step.get("count", 1)))
            time.sleep(LUCKY_ESCAPE_WAIT)
        elif action == "press_arrow":
            pyautogui.press(step["direction"], presses=int(step.get("count", 1)), interval=LUCKY_ARROW_WAIT)
        elif action == "press_home":
            pyautogui.press("home")
        elif action == "press_end":
            pyautogui.press("end")
        elif action == "hotkey":
            keys = step.get("keys") or ()
            pyautogui.hotkey(*keys)
        elif action in ["type_text", "ai_type"]:
            mod = self._primary_mod()
            pyautogui.hotkey(mod, "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.1)
            pyautogui.typewrite("A", interval=LUCKY_TYPE_INTERVAL)
        elif action in ("maximise_window", "maximize"):
            self._maximise()
        elif action == "open_chrome":
            self._trainer_launch_chrome()
            self._activate_chrome()
        elif action == "type":
            self._trainer_type_omnibar(step)
        elif action == "type_whatsapp_number":
            import pyautogui  # type: ignore

            self._os.safe_activate_chrome()
            time.sleep(0.25)
            wn = str(step.get("whatsapp_number") or step.get("type_text") or "5550000000").strip()
            pyautogui.write(wn, interval=0.02)
            time.sleep(LUCKY_CAPTURE_WAIT)
        elif action == "type_completion_message":
            import pyautogui  # type: ignore

            self._os.safe_activate_chrome()
            time.sleep(0.25)
            pyautogui.write("REKKY_COMPLETION_PLACEHOLDER", interval=0.02)
            time.sleep(LUCKY_CAPTURE_WAIT)
        elif action == "close_chrome":
            self._trainer_close_chrome()
            return
        elif action == "close_browser":
            return
        elif action == "wait":
            time.sleep(self._wait_secs(step))

        time.sleep(wait)

    def _exec_step(self, step: WorkflowStep) -> None:
        """
        Backwards-compatible execution hook for tests and harnesses.

        This wraps the dense step execution; callers can monkeypatch this method to avoid
        real keypresses while still exercising Lucky's validation logic.
        """
        self._simulate_step_dense(step)

    def _is_aha_execute_only(self, step: WorkflowStep) -> bool:
        """
        Step-level override:
        - aha_execute_only=true
        - execute_only_in_aha=true
        Lucky should not execute these steps physically.
        """
        return bool(step.get("aha_execute_only") or step.get("execute_only_in_aha"))

    def _step_severity(self, step: WorkflowStep) -> str:
        criticality = str(step.get("criticality") or step.get("risk_level") or "").strip().lower()
        if criticality in {"critical", "high"}:
            return "high"
        if criticality in {"medium", "normal"}:
            return "medium"
        return "low"

    def _decision_from_drift(self, drift_map: list[dict[str, Any]], total_steps: int) -> tuple[str, float, dict[str, int]]:
        summary = {"high": 0, "medium": 0, "low": 0}
        penalty = 0.0
        for row in drift_map:
            sev = str(row.get("severity") or "low").lower()
            if sev not in summary:
                sev = "low"
            summary[sev] += 1
            if row.get("resolved_after_refresh"):
                penalty += {"high": 0.12, "medium": 0.07, "low": 0.03}[sev]
            else:
                penalty += {"high": 0.28, "medium": 0.16, "low": 0.08}[sev]
        base = 1.0 - min(0.95, penalty + (0.02 if total_steps > 40 else 0.0))
        confidence = max(0.05, round(base, 3))
        if summary["high"] >= 1 and any(not x.get("resolved_after_refresh") for x in drift_map):
            return "BLOCK", confidence, summary
        if summary["medium"] >= 2:
            return "GO_WITH_CAUTION", confidence, summary
        if drift_map:
            return "GO_WITH_CAUTION", confidence, summary
        return "GO", confidence, summary

    def _update_health_history(self, report: LuckyReport) -> None:
        history_path = os.path.join(self._logs_dir, "lucky_health_history.json")
        payload: dict[str, Any] = {"entries": []}
        try:
            if os.path.exists(history_path):
                with open(history_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    payload = loaded
        except Exception:
            payload = {"entries": []}
        entries = payload.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries.append(
            {
                "timestamp": report.timestamp,
                "signal": report.signal,
                "go_decision": report.go_decision,
                "confidence_score": report.confidence_score,
                "permanent_mismatches": report.permanent_mismatches,
                "temporary_mismatches": report.temporary_mismatches,
            }
        )
        payload["entries"] = entries[-50:]
        write_json(history_path, payload)

    def run_and_validate_step(
        self,
        step: WorkflowStep,
        steps: list[WorkflowStep],
        idx: int,
        *,
        drift_map: list[dict[str, Any]],
        total_keypresses_validated: list[int],
    ) -> tuple[str | None]:
        """Execute one Lucky step + validation. Returns abort_reason fragment or None."""

        import pyautogui  # type: ignore

        action = step.get("action_type")

        if self._is_aha_execute_only(step):
            expected = dict(step.get("focus_target") or {})
            if expected:
                actual = self._os.capture_active_element() or {}
                total_keypresses_validated[0] += 1
                matched = elements_match(expected, actual)
                self._alog(
                    f"AHA_ONLY_PROBE step={int(step.get('step', idx + 1))} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                    "matched" if matched else "mismatch_non_blocking",
                )
                if not matched:
                    # Non-blocking observation only: this tag means AHA executes, Lucky only probes.
                    drift_map.append(
                        {
                            "step": int(step.get("step", idx + 1)),
                            "position": 1,
                            "key": f"{action}_probe",
                            "expected_element": expected,
                            "actual_element": actual,
                            "resolved_after_refresh": True,
                            "non_blocking_observation": True,
                            "severity": self._step_severity(step),
                        }
                    )
            self._exec_step_spacing(step)
            return None

        # Trainer legacy steps — must run before Tab/Enter validation (same as Rekky enrich).
        if action == "open_chrome":
            self._trainer_launch_chrome()
            self._activate_chrome()
            self._exec_step_spacing(step)
            return None

        if action == "type":
            self._trainer_type_omnibar(step)
            self._exec_step_spacing(step)
            return None

        if action == "type_whatsapp_number":
            self._os.safe_activate_chrome()
            time.sleep(0.25)
            wn = str(step.get("whatsapp_number") or step.get("type_text") or "5550000000").strip()
            pyautogui.write(wn, interval=0.02)
            time.sleep(LUCKY_CAPTURE_WAIT)
            self._exec_step_spacing(step)
            return None

        if action == "type_completion_message":
            self._os.safe_activate_chrome()
            time.sleep(0.25)
            pyautogui.write("REKKY_COMPLETION_PLACEHOLDER", interval=0.02)
            time.sleep(LUCKY_CAPTURE_WAIT)
            self._exec_step_spacing(step)
            return None

        if action == "close_chrome":
            self._trainer_close_chrome()
            self._exec_step_spacing(step)
            return None

        intermediates = list(step.get("intermediate_elements") or [])
        focus_target = step.get("focus_target")

        if action == "press_tab":
            tab_count = int(step.get("tab_count", 1))
            for i in range(1, tab_count + 1):
                pyautogui.press("tab")
                self._alog(f"TAB_PRESS step={int(step.get('step', idx + 1))} position={i}", "pressed")
                time.sleep(LUCKY_TAB_INTERVAL)
                expected = intermediates[i - 1] if i <= len(intermediates) else None
                if not self._lucky_should_capture_tab(i, tab_count, expected):
                    self._alog(
                        f"TAB_SKIP_CAPTURE step={int(step.get('step', idx + 1))} position={i} mode={LUCKY_TAB_CAPTURE_MODE}",
                        "no_anchor",
                    )
                    continue
                time.sleep(LUCKY_CAPTURE_WAIT)
                actual = self._os.capture_active_element()
                if expected is not None:
                    total_keypresses_validated[0] += 1
                    matched = elements_match(expected, actual)
                    self._alog(
                        f"TAB_CAPTURE step={int(step.get('step', idx + 1))} position={i} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')} expected={expected}",
                        "matched" if matched else "mismatch",
                    )
                    if not matched:
                        verdict = self._lucky_refresh_and_recheck_tab(steps, idx, i)
                        drift_map.append(
                            {
                                "step": int(step.get("step", idx + 1)),
                                "position": i,
                                "key": "tab",
                                "expected_element": expected,
                                "actual_element": actual,
                                "resolved_after_refresh": verdict == "TEMPORARY",
                                "severity": self._step_severity(step),
                            }
                        )
                        if verdict == "PERMANENT":
                            self._exec_step_spacing(step)
                            return "permanent"
            self._exec_step_spacing(step)

        elif action == "press_enter":
            if step.get("is_final") or step.get("is_destination"):
                logger.info("Lucky: skipping destination Enter — dry run")
                self._exec_step_spacing(step)
            else:
                pyautogui.press("enter")
                time.sleep(LUCKY_ENTER_WAIT)
                time.sleep(LUCKY_CAPTURE_WAIT)
                actual = self._os.capture_active_element()
                if focus_target:
                    total_keypresses_validated[0] += 1
                    matched = elements_match(focus_target, actual)
                    self._alog(
                        f"ENTER_CAPTURE step={int(step.get('step', idx + 1))} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')} expected={focus_target}",
                        "matched" if matched else "mismatch",
                    )
                    if not matched:
                        result = self._lucky_refresh_and_recheck_enter(steps, idx)
                        drift_map.append(
                            {
                                "step": int(step.get("step", idx + 1)),
                                "position": 1,
                                "key": "enter",
                                "expected_element": focus_target,
                                "actual_element": actual,
                                "resolved_after_refresh": result == "TEMPORARY",
                                "severity": self._step_severity(step),
                            }
                        )
                        if result == "PERMANENT":
                            self._exec_step_spacing(step)
                            return "permanent"
                self._exec_step_spacing(step)

        elif action == "press_arrow":
            cnt = int(step.get("count", 1))
            direction = str(step.get("direction", "down"))
            for i in range(1, cnt + 1):
                pyautogui.press(direction)
                time.sleep(LUCKY_ARROW_WAIT)
                actual = self._os.capture_active_element()
                if i <= len(intermediates):
                    expected = intermediates[i - 1]
                    total_keypresses_validated[0] += 1
                    matched = elements_match(expected, actual)
                    self._alog(
                        f"ARROW_CAPTURE step={int(step.get('step', idx + 1))} position={i} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')} expected={expected}",
                        "matched" if matched else "mismatch",
                    )
                    if not matched:
                        drift_map.append(
                            {
                                "step": int(step.get("step", idx + 1)),
                                "position": i,
                                "key": f"arrow_{direction}",
                                "expected_element": expected,
                                "actual_element": actual,
                                "resolved_after_refresh": False,
                                "severity": self._step_severity(step),
                            }
                        )
                        self._exec_step_spacing(step)
                        return "permanent"
            self._exec_step_spacing(step)

        elif action in ["type_text", "ai_type"]:
            mod = self._primary_mod()
            pyautogui.hotkey(mod, "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.1)
            pyautogui.typewrite("A", interval=LUCKY_TYPE_INTERVAL)
            self._exec_step_spacing(step)

        elif action == "press_escape":
            pyautogui.press("escape", presses=int(step.get("count", 1)))
            time.sleep(LUCKY_ESCAPE_WAIT)
            self._exec_step_spacing(step)

        elif action == "press_space":
            pyautogui.press("space", presses=int(step.get("count", 1)))
            self._exec_step_spacing(step)

        elif action == "press_home":
            pyautogui.press("home")
            time.sleep(0.3)
            self._exec_step_spacing(step)

        elif action == "press_end":
            pyautogui.press("end")
            time.sleep(0.3)
            self._exec_step_spacing(step)

        elif action == "hotkey":
            keys = step.get("keys") or ()
            pyautogui.hotkey(*keys)
            time.sleep(0.3)
            self._exec_step_spacing(step)

        elif action in ("maximise_window", "maximize"):
            self._maximise()
            time.sleep(0.5)
            self._exec_step_spacing(step)

        elif action == "open_url":
            self._open_url_like(step)
            loaded, last_title = self._verify_page_loaded(step, timeout_s=10.0)
            if not loaded:
                self._exec_step_spacing(step)
                return "Page did not load"
            self._exec_step_spacing(step)

        elif action == "wait":
            time.sleep(self._wait_secs(step))
            self._exec_step_spacing(step)

        elif action == "close_browser":
            return None

        else:
            self._exec_step(step)
            self._exec_step_spacing(step)

        return None

    def _refresh(self) -> None:
        import pyautogui  # type: ignore

        mod = self._primary_mod()
        self._alog("REFRESH_ATTEMPT", "started")
        pyautogui.hotkey(mod, "r")
        time.sleep(LUCKY_REFRESH_WAIT)
        self._activate_chrome()
        pyautogui.press("escape")
        time.sleep(ESCAPE_WAIT)
        self._alog("REFRESH_ATTEMPT", "completed")

    def _replay_prefix_steps(self, steps: list[WorkflowStep], start: int, end_exclusive: int) -> None:
        self._activate_chrome()
        for j in range(start, end_exclusive):
            self._exec_step(steps[j])

    def _re_navigate_to_index(self, steps: list[WorkflowStep], idx: int) -> None:
        """
        Backwards-compatible helper used by older Lucky harnesses/tests.

        Replays from the nearest checkpoint (if any) up to the target index.
        """
        target = max(0, int(idx))
        anchor = nearest_checkpoint_index(steps, target + 1)
        starter = anchor if anchor is not None else 0
        self._replay_prefix_steps(steps, starter, target)

    def _validate_step_anchor(
        self, step: WorkflowStep, steps: list[WorkflowStep], idx: int
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """
        Backwards-compatible anchor validator used by unit tests.

        Returns (verdict, expected_element, actual_element) where verdict is one of:
        MATCH | TEMPORARY | PERMANENT.
        """
        _ = steps, idx
        expected = dict(step.get("focus_target") or {})
        actual = self._os.capture_active_element() or {}
        if expected and elements_match(expected, actual):
            return "MATCH", expected, actual
        return "PERMANENT", expected, actual

    def _lucky_should_capture_tab(self, position: int, tab_count: int, expected: dict | None) -> bool:
        mode = (LUCKY_TAB_CAPTURE_MODE or "smart").strip().lower()
        has_anchor = anchor_has_validation_signal(expected)
        if mode == "off":
            return False
        if mode == "all":
            # "all" = every tab that has a Rekky anchor — not AppleScript on blind tab steps
            return has_anchor
        if mode == "ends":
            return has_anchor and position in (1, tab_count)
        # smart (default): only pay for AppleScript when the step has a real anchor
        return has_anchor

    def _lucky_refresh_and_recheck_tab(
        self, steps: list[WorkflowStep], target_idx: int, completed_tabs: int
    ) -> str:
        if not self._refresh_on_mismatch:
            self._alog("REFRESH_ATTEMPT", "skipped_disabled")
            return "PERMANENT"
        middle = steps[target_idx]
        i = completed_tabs
        ims = list(middle.get("intermediate_elements") or [])
        if i - 1 < 0 or i - 1 >= len(ims):
            return "PERMANENT"
        expected = ims[i - 1]
        self._refresh()
        anchor = nearest_checkpoint_index(steps, target_idx + 1)
        starter = anchor if anchor is not None else 0
        self._replay_prefix_steps(steps, starter, target_idx)
        import pyautogui  # type: ignore

        for _ in range(i):
            pyautogui.press("tab")
            time.sleep(LUCKY_TAB_INTERVAL)
            time.sleep(LUCKY_CAPTURE_WAIT)
        actual = self._os.capture_active_element()
        if elements_match(expected, actual):
            return "TEMPORARY"
        return "PERMANENT"

    def _lucky_refresh_and_recheck_enter(self, steps: list[WorkflowStep], target_idx: int) -> str:
        if not self._refresh_on_mismatch:
            self._alog("REFRESH_ATTEMPT", "skipped_disabled")
            return "PERMANENT"
        middle = steps[target_idx]
        focus_target = middle.get("focus_target")
        self._refresh()
        anchor = nearest_checkpoint_index(steps, target_idx + 1)
        starter = anchor if anchor is not None else 0
        self._replay_prefix_steps(steps, starter, target_idx)
        import pyautogui  # type: ignore

        pyautogui.press("enter")
        time.sleep(LUCKY_ENTER_WAIT)
        actual = self._os.capture_active_element()
        if focus_target and elements_match(focus_target, actual):
            return "TEMPORARY"
        return "PERMANENT"

    def run(self, workflow: WorkflowJson) -> LuckyReport:
        steps: list[WorkflowStep] = list(workflow.get("steps") or [])
        drift_map: list[dict[str, Any]] = []
        type_steps: list[int] = []
        self._executed_step_actions = 0
        total_keypresses_validated = [0]

        abort_reason = ""

        def _finalize(signal: str, reason: str) -> LuckyReport:
            total_mismatch = len(drift_map)
            permanent_cnt = sum(1 for x in drift_map if not x.get("resolved_after_refresh"))
            go_decision, confidence_score, severity_summary = self._decision_from_drift(drift_map, len(steps))
            rep = LuckyReport(
                signal=signal,
                drift_map=drift_map,
                type_steps=type_steps,
                total_rekky_steps=len(steps),
                total_lucky_steps=self._executed_step_actions,
                global_drift_delta=total_mismatch,
                abort_reason=reason,
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                total_keypresses_validated=total_keypresses_validated[0],
                total_mismatches=total_mismatch,
                permanent_mismatches=permanent_cnt,
                temporary_mismatches=sum(1 for x in drift_map if x.get("resolved_after_refresh")),
                go_decision=go_decision,
                confidence_score=confidence_score,
                severity_summary=severity_summary,
            )
            os.makedirs(self._logs_dir, exist_ok=True)
            write_json(os.path.join(self._logs_dir, f"lucky_report_{ts_compact()}.json"), rep.to_dict())
            self._update_health_history(rep)
            self._alog("FINAL_SIGNAL", f"{signal} reason={reason}")
            return rep

        for idx, step in enumerate(steps):
            if self._stop_event is not None and self._stop_event.is_set():
                abort_reason = "Cancelled by user"
                break

            action = step.get("action_type")
            if action in ["type_text", "ai_type"]:
                type_steps.append(int(step.get("step", idx + 1)))

            # Simple focus-target validation path: used when steps define a focus_target anchor but
            # do not have intermediate elements. This also enables deterministic unit testing via
            # monkeypatching _exec_step / _validate_step_anchor.
            focus_target = step.get("focus_target")
            use_simple_anchor_validation = (
                focus_target
                and not list(step.get("intermediate_elements") or [])
                and action in ("press_tab", "press_enter", "press_arrow")
                and not (action == "press_enter" and (step.get("is_final") or step.get("is_destination")))
                and not self._is_aha_execute_only(step)
            )
            if use_simple_anchor_validation:
                try:
                    self._exec_step(step)
                except Exception:
                    pass
                verdict, expected, actual = self._validate_step_anchor(step, steps, idx)
                total_keypresses_validated[0] += 1
                if verdict != "MATCH":
                    drift_map.append(
                        {
                            "step": int(step.get("step", idx + 1)),
                            "position": 1,
                            "key": str(action or ""),
                            "expected_element": expected,
                            "actual_element": actual,
                            "resolved_after_refresh": verdict == "TEMPORARY",
                            "severity": self._step_severity(step),
                        }
                    )
                self._exec_step_spacing(step)
            else:
                step_abort = self.run_and_validate_step(
                    step, steps, idx, drift_map=drift_map, total_keypresses_validated=total_keypresses_validated
                )
                if step_abort:
                    abort_reason = str(step_abort)
                    break

            permanent_mismatch_count = sum(1 for x in drift_map if not x.get("resolved_after_refresh"))
            if permanent_mismatch_count >= 2:
                abort_reason = (
                    f"Permanent drift threshold reached (2+). "
                    f"Mismatches (permanent-only count)≥2 at/near step {int(step.get('step', idx + 1))}"
                )
                break

        if abort_reason:
            return _finalize("ABORT", abort_reason)

        import pyautogui  # type: ignore

        mod = self._primary_mod()

        for idx, step in enumerate(steps):
            if step.get("action_type") not in ["type_text", "ai_type"]:
                continue
            self._refresh()
            anchor = nearest_checkpoint_index(steps, idx + 1)
            starter = anchor if anchor is not None else 0
            self._replay_prefix_steps(steps, starter, idx + 1)
            pyautogui.hotkey(mod, "a")
            time.sleep(0.1)
            pyautogui.press("delete")
            time.sleep(0.1)

        self._refresh()

        report = _finalize("GREEN", "")
        logger.info("Lucky OK: mismatches recorded=%s", len(drift_map))
        return report
