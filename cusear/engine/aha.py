from __future__ import annotations

import logging
import platform as plat_module
import subprocess
import time
from typing import Any

from .constants import (
    AHA_ARROW_INTERVAL,
    AHA_BETWEEN_STEPS,
    AHA_ESCAPE_WAIT,
    AHA_POST_SETTLE,
    AHA_TAB_INTERVAL,
    AHA_TYPE_INTERVAL,
    AHA_URL_LOAD_WAIT,
    MOVE_TIMEOUT,
)
from .os_adapter import get_os_adapter, open_url_in_google_chrome
from .reporting import send_to_company
from .screenshot import capture_screenshot
from .session_steps import SessionSteps
from .shared_state import SharedState

logger = logging.getLogger(__name__)


class AHA:
    """
    Artificial Human Agent — executor (user-facing keystrokes).

    Executes per MOVE signal from Agami; emits LANDED_N after each atomic key landing.
    """

    def __init__(
        self,
        *,
        company_endpoint: str | None,
        company_logs_dir: str,
        screenshots_dir: str,
        notify_success: Any | None = None,
        notify_failure: Any | None = None,
    ) -> None:
        self._os = get_os_adapter()
        self._company_endpoint = company_endpoint
        self._company_logs_dir = company_logs_dir
        self._screenshots_dir = screenshots_dir
        self._notify_success = notify_success or (lambda: None)
        self._notify_failure = notify_failure or (lambda _msg="": None)

    def _primary_mod(self) -> str:
        return "command" if plat_module.system() == "Darwin" else "ctrl"

    def _get_content(self, step: WorkflowStep, content_map: dict[str, Any]) -> str:
        key = step.get("content_key") or step.get("value") or ""
        if isinstance(key, str) and key and key in content_map:
            val = content_map.get(key, "")
            return "" if val is None else str(val)
        val = step.get("value", "")
        return "" if val is None else str(val)

    def execute(self, session: SessionSteps, content_map: dict[str, Any], shared: SharedState) -> bool:
        import pyautogui  # type: ignore

        i = 0
        while i < len(session):
            if shared.abort:
                self._notify_failure(shared.abort_reason)
                return False

            step = session.get(i)
            action = step.get("action_type")
            step_num = int(step.get("step", i + 1))

            try:

                def _after_landed_requires_ack(require_ack: bool) -> bool:
                    if not require_ack:
                        return True
                    if shared.wait_landed_processed(timeout=MOVE_TIMEOUT):
                        return True
                    capture_screenshot(f"{self._screenshots_dir}/aha_land_ack_timeout_{step_num}.png")
                    shared.send_abort(f"LANDED processed timeout at step {step_num}")
                    return False

                def _must_move(signal: str) -> bool:
                    if signal == "ABORT":
                        return False
                    if signal != "MOVE":
                        if signal == "TIMEOUT":
                            capture_screenshot(f"{self._screenshots_dir}/aha_timeout_{step_num}.png")
                            send_to_company(
                                company_endpoint=self._company_endpoint,
                                logs_dir=self._company_logs_dir,
                                trigger="move_timeout",
                                step=step_num,
                                extra_notes="Timeout waiting for MOVE.",
                            )
                            shared.send_abort(reason=f"MOVE timeout at step {step_num}")
                        return False
                    return True

                if action == "open_url":
                    sig = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if not _must_move(sig):
                        return False
                    open_url_in_google_chrome(str(step.get("url") or ""))
                    time.sleep(AHA_URL_LOAD_WAIT)
                    self._os.safe_activate_chrome()
                    pyautogui.press("escape", presses=2)
                    time.sleep(AHA_ESCAPE_WAIT)
                    if plat_module.system() == "Darwin":
                        pyautogui.hotkey("command", "control", "f")
                    else:
                        pyautogui.hotkey("win", "up")
                    time.sleep(0.5)
                    pyautogui.press("escape", presses=2)
                    time.sleep(AHA_ESCAPE_WAIT)
                    shared.send_done()

                elif action == "press_tab":
                    tc = max(1, int(step.get("tab_count", 1)))
                    for tab_i in range(1, tc + 1):
                        sig2 = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                        if sig2 == "ABORT":
                            self._notify_failure(shared.abort_reason)
                            return False
                        if sig2 != "MOVE":
                            capture_screenshot(f"{self._screenshots_dir}/aha_timeout_{step_num}_{tab_i}.png")
                            shared.send_abort(f"MOVE timeout at step {step_num} position {tab_i}")
                            return False
                        pyautogui.press("tab")
                        time.sleep(AHA_TAB_INTERVAL)
                        shared.send_landed_at(tab_i)
                        if not _after_landed_requires_ack(True):
                            return False
                    shared.send_done()

                elif action == "press_enter":
                    sig_e = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_e == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_e):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    pyautogui.press("enter")
                    time.sleep(AHA_POST_SETTLE)
                    shared.send_landed_at(1)
                    if not _after_landed_requires_ack(True):
                        return False

                    if step.get("is_final") or step.get("is_destination"):
                        time.sleep(1.0)
                        screenshot_path = f"{self._screenshots_dir}/aha_success_{step_num}.png"
                        capture_screenshot(screenshot_path)
                        send_to_company(
                            company_endpoint=self._company_endpoint,
                            logs_dir=self._company_logs_dir,
                            trigger="post_success",
                            step=step_num,
                            extra_notes=f"Destination Enter executed. Screenshot: {screenshot_path}",
                        )
                        self._notify_success()
                    shared.send_done()

                elif action == "press_space":
                    sig_s = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_s == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_s):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    pyautogui.press("space", presses=int(step.get("count", 1)))
                    time.sleep(AHA_POST_SETTLE)
                    shared.send_landed_at(1)
                    if not _after_landed_requires_ack(True):
                        return False
                    shared.send_done()

                elif action == "press_escape":
                    sig_esc = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_esc == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_esc):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    pyautogui.press("escape", presses=int(step.get("count", 1)))
                    time.sleep(AHA_ESCAPE_WAIT)
                    shared.send_landed_at(1)
                    if not _after_landed_requires_ack(True):
                        return False
                    shared.send_done()

                elif action == "press_arrow":
                    cnt = max(1, int(step.get("count", 1)))
                    direction = str(step.get("direction", "down"))
                    for ai in range(1, cnt + 1):
                        sig_ar = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                        if sig_ar == "ABORT":
                            self._notify_failure(shared.abort_reason)
                            return False
                        if not _must_move(sig_ar):
                            self._notify_failure(shared.abort_reason or "MOVE timeout")
                            return False
                        pyautogui.press(direction)
                        time.sleep(AHA_ARROW_INTERVAL)
                        shared.send_landed_at(ai)
                        if not _after_landed_requires_ack(True):
                            return False
                    shared.send_done()

                elif action == "press_home":
                    sig_h = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_h == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_h):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    pyautogui.press("home")
                    time.sleep(0.3)
                    shared.send_landed_at(1)
                    if not _after_landed_requires_ack(True):
                        return False
                    shared.send_done()

                elif action == "press_end":
                    sig_en = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_en == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_en):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    pyautogui.press("end")
                    time.sleep(0.3)
                    shared.send_landed_at(1)
                    if not _after_landed_requires_ack(True):
                        return False
                    shared.send_done()

                elif action == "hotkey":
                    sig_hk = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_hk == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_hk):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    keys = step.get("keys") or []
                    pyautogui.hotkey(*keys)
                    time.sleep(0.3)
                    shared.send_landed_at(1)
                    if not _after_landed_requires_ack(True):
                        return False
                    shared.send_done()

                elif action in ["type_text", "ai_type"]:
                    sig_t = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_t == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_t):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    mod = self._primary_mod()
                    pyautogui.hotkey(mod, "a")
                    time.sleep(0.1)
                    pyautogui.press("delete")
                    time.sleep(0.1)
                    content = self._get_content(step, content_map)
                    pyautogui.typewrite(content, interval=AHA_TYPE_INTERVAL)
                    shared.send_done()

                elif action == "maximise_window":
                    sig_mx = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_mx == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_mx):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    if plat_module.system() == "Darwin":
                        pyautogui.hotkey("command", "control", "f")
                    else:
                        pyautogui.hotkey("win", "up")
                    time.sleep(0.5)
                    shared.send_done()

                elif action == "close_browser":
                    sig_cb = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_cb == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_cb):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    if plat_module.system() == "Darwin":
                        subprocess.run(
                            [
                                "osascript",
                                "-e",
                                'tell application "Google Chrome" to close every window',
                            ],
                            timeout=3,
                        )
                    else:
                        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], timeout=5)
                    shared.send_done()

                elif action == "wait":
                    sig_w = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_w == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_w):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    time.sleep(float(step.get("duration", 1.0)))
                    shared.send_done()

                else:
                    sig_o = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_o == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_o):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    shared.send_done()

                time.sleep(AHA_BETWEEN_STEPS)
                i += 1

            except Exception as e:
                screenshot_path = f"{self._screenshots_dir}/aha_error_{step_num}.png"
                capture_screenshot(screenshot_path)
                send_to_company(
                    company_endpoint=self._company_endpoint,
                    logs_dir=self._company_logs_dir,
                    trigger="runtime_exception",
                    step=step_num,
                    extra_notes=f"{e}. Screenshot: {screenshot_path}",
                )
                self._notify_failure(str(e))
                shared.send_abort(reason=str(e))
                return False

        return True
