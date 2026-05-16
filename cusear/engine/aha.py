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
    AHA_MIN_GAP_SEC,
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
from .workflow import WorkflowStep

logger = logging.getLogger(__name__)


def _aha_gap(seconds: float) -> float:
    """Minimum pause between AHA steps and between each tab press."""
    return max(float(AHA_MIN_GAP_SEC), float(seconds))


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
        require_landed_ack: bool = True,
        workflow_name: str = "",
        notify_success: Any | None = None,
        notify_failure: Any | None = None,
    ) -> None:
        self._os = get_os_adapter()
        self._company_endpoint = company_endpoint
        self._company_logs_dir = company_logs_dir
        self._screenshots_dir = screenshots_dir
        self._require_landed_ack = bool(require_landed_ack)
        self._workflow_name = str(workflow_name or "").strip()
        self._notify_success = notify_success or (lambda: None)
        self._notify_failure = notify_failure or (lambda _msg="": None)
        self._action_logger = self._setup_action_logger()

    def _setup_action_logger(self) -> logging.Logger:
        import os

        logs_dir = os.path.join(os.path.dirname(self._company_logs_dir), "aha")
        os.makedirs(logs_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(logs_dir, f"aha_{stamp}.log")
        name = f"cusear.aha.actions.{stamp}.{id(self)}"
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d [AHA] ACTION=%(message)s",
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
        return "command" if plat_module.system() == "Darwin" else "ctrl"

    def _get_content(self, step: WorkflowStep, content_map: dict[str, Any]) -> str:
        key = step.get("content_key") or step.get("value") or ""
        if isinstance(key, str) and key and key in content_map:
            val = content_map.get(key, "")
            return "" if val is None else str(val)
        val = step.get("value", "")
        return "" if val is None else str(val)

    def _verify_success_evidence(self, step: WorkflowStep) -> bool:
        """
        Optional evidence validation.
        Step may include:
        - success_evidence: ["posted", "published", ...]
        """
        expected = step.get("success_evidence")
        tokens: list[str] = []
        if isinstance(expected, list):
            tokens.extend(str(x).strip().lower() for x in expected if str(x).strip())
        platform_hint = str(step.get("platform_hint") or "").strip().lower()
        platform_defaults = {
            "facebook": ["posted", "shared"],
            "instagram": ["posted", "share"],
            "linkedin": ["post", "posted", "shared"],
            "x": ["post", "posted", "sent"],
            "twitter": ["post", "posted", "sent"],
            "whatsapp": ["sent", "message", "status"],
        }
        for tok in platform_defaults.get(platform_hint, []):
            if tok not in tokens:
                tokens.append(tok)
        if not tokens:
            return True
        actual = self._os.capture_active_element() or {}
        hay = " ".join(
            [
                str(actual.get("text") or ""),
                str(actual.get("role") or ""),
                str(actual.get("id") or ""),
                str(actual.get("className") or ""),
            ]
        ).lower()
        if not hay.strip():
            return False
        return any(token in hay for token in tokens)

    def _already_published(self, step: WorkflowStep) -> bool:
        if not bool(step.get("idempotent_guard")):
            return False
        actual = self._os.capture_active_element() or {}
        hay = " ".join([str(actual.get("text") or ""), str(actual.get("role") or "")]).lower()
        return any(tok in hay for tok in ("posted", "published", "sent", "shared"))

    def _typing_interval(self, content: str) -> float:
        base = float(AHA_TYPE_INTERVAL)
        if len(content) > 500:
            return min(0.03, base * 1.8)
        if len(content) > 200:
            return min(0.025, base * 1.4)
        return base

    def _wait_secs(self, step: WorkflowStep) -> float:
        if step.get("wait_seconds") is not None:
            return _aha_gap(float(step.get("wait_seconds") or 1.0))
        if step.get("wait") is not None:
            return _aha_gap(float(step.get("wait") or 1.0))
        return _aha_gap(float(step.get("duration") or 1.0))

    def _expected_for(self, step: WorkflowStep, position: int) -> tuple[str, str, str]:
        items = step.get("intermediate_elements") or []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    if int(it.get("position") or 0) != int(position):
                        continue
                except Exception:
                    continue
                return (
                    str(it.get("tagName") or ""),
                    str(it.get("text") or ""),
                    str(it.get("role") or ""),
                )
        ft = step.get("focus_target") if isinstance(step.get("focus_target"), dict) else {}
        return (
            str(ft.get("tagName") or ""),
            str(ft.get("text") or ""),
            str(ft.get("role") or ""),
        )

    def _log_element_snapshot(self, step_num: int, position: int, step: WorkflowStep) -> None:
        try:
            from .constants import AHA_DOM_SETTLE

            time.sleep(AHA_DOM_SETTLE)
            actual = self._os.capture_active_element() or {}
        except Exception:
            actual = {}
        exp_tag, exp_text, exp_role = self._expected_for(step, position)
        self._alog(
            (
                f"ELEMENT_SNAPSHOT step={step_num} position={position} "
                f"actual_tag='{str(actual.get('tagName') or '')}' "
                f"actual_text='{str(actual.get('text') or '')}' "
                f"actual_role='{str(actual.get('role') or '')}' "
                f"expected_tag='{exp_tag}' expected_text='{exp_text}' expected_role='{exp_role}'"
            ),
            "captured",
        )

    def execute(self, session: SessionSteps, content_map: dict[str, Any], shared: SharedState) -> bool:
        import pyautogui  # type: ignore

        from .step_bridge import StepBridgeAbort, StepBridgeTimeout, gate_before_step

        i = 0
        while i < len(session):
            if shared.abort:
                self._alog("FINAL", f"failure abort_reason={shared.abort_reason}")
                self._notify_failure(shared.abort_reason)
                return False

            step = session.get(i)
            action = step.get("action_type")
            step_num = int(step.get("step", i + 1))
            total_steps = len(session)

            try:
                gate_before_step(
                    phase="aha",
                    workflow_name=self._workflow_name,
                    step_index=i + 1,
                    total_steps=total_steps,
                    step_number=step_num,
                    action_type=str(action or ""),
                )
            except StepBridgeAbort as exc:
                shared.send_abort(str(exc))
                self._notify_failure(str(exc))
                return False
            except StepBridgeTimeout as exc:
                shared.send_abort(str(exc))
                self._notify_failure(str(exc))
                return False

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
                    self._alog(f"MOVE_RECEIVED step={step_num} signal={signal}", "received")
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
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if not _must_move(sig):
                        return False
                    open_url_in_google_chrome(str(step.get("url") or ""))
                    time.sleep(AHA_URL_LOAD_WAIT)
                    self._os.safe_activate_chrome()
                    pyautogui.press("escape", presses=2)
                    time.sleep(AHA_ESCAPE_WAIT)
                    if plat_module.system() == "Darwin":
                        # Avoid command/control/f on macOS to prevent opening browser Find box.
                        pass
                    else:
                        pyautogui.hotkey("win", "up")
                    time.sleep(0.5)
                    pyautogui.press("escape", presses=2)
                    time.sleep(AHA_ESCAPE_WAIT)
                    shared.send_done()
                    self._log_element_snapshot(step_num, 1, step)
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "open_chrome":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_oc = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_oc == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_oc):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    if plat_module.system() == "Darwin":
                        subprocess.Popen(["open", "-a", "Google Chrome"])
                    elif plat_module.system() == "Windows":
                        subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
                    else:
                        subprocess.Popen(["google-chrome"])
                    time.sleep(1.5)
                    self._os.safe_activate_chrome()
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "type":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_ty = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_ty == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_ty):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    mod = self._primary_mod()
                    self._os.safe_activate_chrome()
                    time.sleep(0.35)
                    txt = str(step.get("type_text") or "").strip()
                    if not txt and str(step.get("description") or "").strip().lower().startswith("http"):
                        txt = str(step.get("description") or "").strip()
                    pyautogui.hotkey(mod, "l")
                    time.sleep(0.28)
                    if txt:
                        pyautogui.hotkey(mod, "a")
                        time.sleep(0.06)
                        pyautogui.press("backspace")
                        pyautogui.write(txt, interval=0.015)
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "type_whatsapp_number":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_tn = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_tn == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_tn):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    self._os.safe_activate_chrome()
                    time.sleep(0.25)
                    wn = str(step.get("whatsapp_number") or step.get("type_text") or "5550000000").strip()
                    pyautogui.write(wn, interval=0.02)
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "type_completion_message":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_tm = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_tm == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_tm):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    self._os.safe_activate_chrome()
                    time.sleep(0.25)
                    pyautogui.write("REKKY_COMPLETION_PLACEHOLDER", interval=0.02)
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "close_chrome":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_cc = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_cc == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_cc):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    try:
                        if plat_module.system() == "Darwin":
                            subprocess.run(
                                ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                                timeout=15,
                                capture_output=True,
                            )
                        elif plat_module.system() == "Windows":
                            subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], timeout=8, capture_output=True)
                    except Exception:
                        pass
                    time.sleep(0.8)
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_tab":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                        self._alog(f"TAB_EXECUTE step={step_num} position={tab_i}", "pressed")
                        time.sleep(_aha_gap(AHA_TAB_INTERVAL))
                        shared.send_landed_at(tab_i)
                        self._alog(f"LANDED_SEND step={step_num} position={tab_i}", "sent")
                        self._log_element_snapshot(step_num, tab_i, step)
                        if not _after_landed_requires_ack(self._require_landed_ack):
                            return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_enter":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_e = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_e == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_e):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    if self._already_published(step):
                        logger.info("AHA: idempotent guard skip enter at step %s", step_num)
                    else:
                        pyautogui.press("enter")
                        time.sleep(_aha_gap(AHA_POST_SETTLE))
                        shared.send_landed_at(1)
                        self._alog(f"LANDED_SEND step={step_num} position=1", "sent")
                        self._log_element_snapshot(step_num, 1, step)
                        if not _after_landed_requires_ack(self._require_landed_ack):
                            return False

                    if step.get("is_final") or step.get("is_destination"):
                        time.sleep(1.0)
                        screenshot_path = f"{self._screenshots_dir}/aha_success_{step_num}.png"
                        capture_screenshot(screenshot_path)
                        evidence_ok = self._verify_success_evidence(step)
                        send_to_company(
                            company_endpoint=self._company_endpoint,
                            logs_dir=self._company_logs_dir,
                            trigger="post_success" if evidence_ok else "post_success_unverified",
                            step=step_num,
                            extra_notes=(
                                f"Destination Enter executed. Screenshot: {screenshot_path}"
                                if evidence_ok
                                else f"Destination Enter executed but evidence missing. Screenshot: {screenshot_path}"
                            ),
                        )
                        if evidence_ok:
                            self._notify_success()
                            self._alog("FINAL", f"success final_step={step_num}")
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_space":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_s = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_s == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_s):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    pyautogui.press("space", presses=int(step.get("count", 1)))
                    time.sleep(_aha_gap(AHA_POST_SETTLE))
                    shared.send_landed_at(1)
                    self._alog(f"LANDED_SEND step={step_num} position=1", "sent")
                    self._log_element_snapshot(step_num, 1, step)
                    if not _after_landed_requires_ack(self._require_landed_ack):
                        return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_escape":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                    self._alog(f"LANDED_SEND step={step_num} position=1", "sent")
                    self._log_element_snapshot(step_num, 1, step)
                    if not _after_landed_requires_ack(self._require_landed_ack):
                        return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_arrow":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                        time.sleep(_aha_gap(AHA_ARROW_INTERVAL))
                        shared.send_landed_at(ai)
                        self._alog(f"LANDED_SEND step={step_num} position={ai}", "sent")
                        self._log_element_snapshot(step_num, ai, step)
                        if not _after_landed_requires_ack(self._require_landed_ack):
                            return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_home":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                    self._alog(f"LANDED_SEND step={step_num} position=1", "sent")
                    self._log_element_snapshot(step_num, 1, step)
                    if not _after_landed_requires_ack(self._require_landed_ack):
                        return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "press_end":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                    self._alog(f"LANDED_SEND step={step_num} position=1", "sent")
                    self._log_element_snapshot(step_num, 1, step)
                    if not _after_landed_requires_ack(self._require_landed_ack):
                        return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "hotkey":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                    self._alog(f"LANDED_SEND step={step_num} position=1", "sent")
                    self._log_element_snapshot(step_num, 1, step)
                    if not _after_landed_requires_ack(self._require_landed_ack):
                        return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action in ["type_text", "ai_type"]:
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                    pyautogui.typewrite(content, interval=self._typing_interval(content))
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", f"done chars={len(content)}")

                elif action == "maximise_window":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_mx = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_mx == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_mx):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    if plat_module.system() == "Darwin":
                        # Avoid command/control/f on macOS to prevent opening browser Find box.
                        pass
                    else:
                        pyautogui.hotkey("win", "up")
                    time.sleep(0.5)
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "close_browser":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
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
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                elif action == "wait":
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_w = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_w == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_w):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    time.sleep(self._wait_secs(step))
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                else:
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "started")
                    sig_o = shared.wait_for_move(timeout=MOVE_TIMEOUT)
                    if sig_o == "ABORT":
                        self._notify_failure(shared.abort_reason)
                        return False
                    if not _must_move(sig_o):
                        self._notify_failure(shared.abort_reason or "MOVE timeout")
                        return False
                    shared.send_done()
                    self._alog(f"PAYLOAD_EXECUTE step={step_num} action={action}", "done")

                time.sleep(_aha_gap(AHA_BETWEEN_STEPS))
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
                self._alog("FINAL", f"failure exception={e}")
                return False

        self._alog("FINAL", "success completed_all_steps")
        return True
