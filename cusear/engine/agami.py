from __future__ import annotations

import logging
import time
from typing import Any

from .constants import (
    AGAMI_ARROW_INTERVAL,
    AGAMI_ESCAPE_WAIT,
    AGAMI_POST_SETTLE,
    AGAMI_SEEK_WAIT,
    AGAMI_TAB_INTERVAL,
    DONE_TIMEOUT,
    HOME_SETTLE,
    LANDED_TIMEOUT,
    MAX_SEEK_BACKWARD,
    MAX_SEEK_FORWARD,
    ESCAPE_WAIT,
)
from .os_adapter import get_os_adapter
from .reporting import send_to_company
from .screenshot import capture_screenshot
from .session_steps import SessionSteps
from .shared_state import SharedState
from .workflow import WorkflowStep, elements_match

logger = logging.getLogger(__name__)


class Agami:
    """
    Session healer. Validates landed elements against Rekky-enriched anchors and heals drift.
    """

    def __init__(
        self,
        *,
        company_endpoint: str | None,
        company_logs_dir: str,
        screenshots_dir: str,
    ) -> None:
        self._os = get_os_adapter()
        self._company_endpoint = company_endpoint
        self._company_logs_dir = company_logs_dir
        self._screenshots_dir = screenshots_dir

    def opening_sequence(self) -> None:
        import pyautogui  # type: ignore

        self._os.safe_activate_chrome()
        pyautogui.press("home")
        time.sleep(HOME_SETTLE)
        self._os.safe_activate_chrome()

    def _capture(self) -> dict[str, str]:
        return self._os.capture_active_element()

    def _seek_tabs_forward_only(self, expected: dict[str, Any]) -> bool:
        import pyautogui  # type: ignore

        for _fwd in range(1, MAX_SEEK_FORWARD + 1):
            pyautogui.press("tab")
            time.sleep(AGAMI_SEEK_WAIT)
            if elements_match(expected, self._capture()):
                logger.info("[Agami] Found forward tab +%s", _fwd)
                return True
        return False

    def _seek_tabs_backward_then_forward(self, expected: dict[str, Any]) -> bool:
        import pyautogui  # type: ignore

        restore = MAX_SEEK_FORWARD + MAX_SEEK_BACKWARD
        for _ in range(restore):
            pyautogui.hotkey("shift", "tab")
            time.sleep(AGAMI_SEEK_WAIT)
        for _bk in range(1, MAX_SEEK_BACKWARD + 1):
            if elements_match(expected, self._capture()):
                logger.info("[Agami] Found backward window near -%s", _bk)
                return True
            pyautogui.press("tab")
            time.sleep(AGAMI_SEEK_WAIT)
        return False

    def _seek_arrows(self, expected: dict[str, Any], direction: str) -> bool:
        import pyautogui  # type: ignore

        for _ in range(1, 11):
            pyautogui.press(direction)
            time.sleep(AGAMI_SEEK_WAIT)
            if elements_match(expected, self._capture()):
                return True
        return False

    def _try_checkpoint_recovery(
        self,
        session: SessionSteps,
        *,
        anchor_index: int,
        checkpoint_index: int,
    ) -> bool:
        import pyautogui  # type: ignore

        pyautogui.press("home")
        time.sleep(HOME_SETTLE)
        self._os.safe_activate_chrome()
        snapshot_len = len(session)
        hi = anchor_index if anchor_index < snapshot_len else snapshot_len - 1
        j = checkpoint_index
        while j <= hi:
            replay_step = session.get(j)
            r_action = replay_step.get("action_type")
            if r_action == "press_tab":
                pyautogui.press("tab", presses=int(replay_step.get("tab_count", 1)), interval=AGAMI_TAB_INTERVAL)
                time.sleep(AGAMI_POST_SETTLE)
            elif r_action == "press_enter":
                if not (replay_step.get("is_final") or replay_step.get("is_destination")):
                    pyautogui.press("enter")
                    time.sleep(AGAMI_POST_SETTLE)
            elif r_action == "press_arrow":
                pyautogui.press(
                    replay_step["direction"],
                    presses=int(replay_step.get("count", 1)),
                    interval=AGAMI_ARROW_INTERVAL,
                )
                time.sleep(AGAMI_POST_SETTLE)
            elif r_action == "hotkey":
                pyautogui.hotkey(*replay_step.get("keys", []))
                time.sleep(AGAMI_POST_SETTLE)
            elif r_action == "press_escape":
                pyautogui.press("escape", presses=int(replay_step.get("count", 1)))
                time.sleep(ESCAPE_WAIT)
            elif r_action == "wait":
                time.sleep(float(replay_step.get("duration", 1.0)))
            j += 1

        anchor = session.get(anchor_index)
        tgt = anchor.get("focus_target")
        if tgt and elements_match(dict(tgt), self._capture()):
            return True
        inter = anchor.get("intermediate_elements") or []
        if inter and isinstance(inter[0], dict) and elements_match(dict(inter[0]), self._capture()):
            return True
        return False

    def walk(self, session: SessionSteps, shared: SharedState) -> None:
        import pyautogui  # type: ignore

        self.opening_sequence()

        i = 0
        last_checkpoint_index: int | None = None

        while i < len(session):
            if shared.abort:
                return

            step = session.get(i)
            action = str(step.get("action_type") or "")
            intermediates = list(step.get("intermediate_elements") or [])
            focus_target = step.get("focus_target")
            anchor = dict(focus_target) if focus_target else None
            step_num = int(step.get("step", i + 1))

            if step.get("is_checkpoint"):
                last_checkpoint_index = i

            if action in ["open_url", "wait", "maximise_window", "close_browser", "type_text", "ai_type"]:
                shared.send_move()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_tab":
                expected = dict(intermediates[0]) if intermediates else (anchor if anchor else None)
                shared.send_move()
                fired, _pos = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout at step {step_num}")
                    return
                actual = self._capture()

                healed = False
                if expected and not elements_match(expected, actual):
                    logger.warning("[Agami] Drift tab step %s", step_num)
                    session.insert_extra_tab_at(i)
                    healed = self._seek_tabs_forward_only(dict(expected))

                    alt_path = step.get("alternate_path")
                    alt_list = alt_path if isinstance(alt_path, list) else []

                    if not healed:
                        healed = self._seek_tabs_backward_then_forward(dict(expected))

                    if not healed and alt_list and self._try_alternate_physical(alt_list):
                        session.replace_step_with_alternate(i + 1, alt_list)
                        healed = True

                    if (
                        not healed
                        and last_checkpoint_index is not None
                        and last_checkpoint_index <= i
                        and self._try_checkpoint_recovery(
                            session,
                            anchor_index=i + 1,
                            checkpoint_index=last_checkpoint_index,
                        )
                    ):
                        healed = True

                    if not healed:
                        p = self._screenshots_dir + f"/agami_abort_{step_num}.png"
                        capture_screenshot(p)
                        send_to_company(
                            company_endpoint=self._company_endpoint,
                            logs_dir=self._company_logs_dir,
                            trigger="agami_seek_failed",
                            step=step_num,
                            expected=expected,
                            actual=actual,
                            extra_notes=f"Screenshot: {p}",
                        )
                        shared.send_abort(reason=f"Element not found at step {step_num}")
                        return

                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_arrow":
                direction = str(step.get("direction", "down"))
                expected = dict(intermediates[0]) if intermediates else (anchor if anchor else None)
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout arrow step {step_num}")
                    return
                actual = self._capture()

                healed = False
                if expected and not elements_match(dict(expected), actual):
                    healed = self._seek_arrows(dict(expected), direction)

                if not healed and expected and not elements_match(dict(expected), actual):
                    send_to_company(
                        company_endpoint=self._company_endpoint,
                        logs_dir=self._company_logs_dir,
                        trigger="agami_seek_failed",
                        step=step_num,
                        expected=expected,
                        actual=actual,
                    )
                    shared.send_abort(f"Arrow destination not found at step {step_num}")
                    return

                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_escape":
                expected = dict(intermediates[0]) if intermediates else None
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout escape step {step_num}")
                    return
                actual = self._capture()
                if expected and not elements_match(expected, actual):
                    if not (
                        self._seek_tabs_forward_only(expected)
                        or self._seek_tabs_backward_then_forward(expected)
                    ):
                        shared.send_abort(f"Escape focus mismatch step {step_num}")
                        return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_space":
                expected = dict(intermediates[0]) if intermediates else None
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout space step {step_num}")
                    return
                actual = self._capture()
                if expected and not elements_match(expected, actual):
                    shared.send_abort(f"Space landing mismatch step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_home":
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout home step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_end":
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout end step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "hotkey":
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout hotkey step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_enter":
                if step.get("is_final") or step.get("is_destination"):
                    tgt = dict(focus_target) if focus_target else None
                    if tgt:
                        actual = self._capture()
                        if not elements_match(tgt, actual):
                            screenshot_path = f"{self._screenshots_dir}/agami_final_{step_num}.png"
                            capture_screenshot(screenshot_path)
                            send_to_company(
                                company_endpoint=self._company_endpoint,
                                logs_dir=self._company_logs_dir,
                                trigger="agami_final_mismatch",
                                step=step_num,
                                expected=tgt,
                                actual=actual,
                                extra_notes=screenshot_path,
                            )
                            shared.send_abort("Final destination element mismatch")
                            return
                    shared.send_move()
                    fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                    if not fired:
                        shared.send_abort(f"LANDED timeout final enter step {step_num}")
                        return
                    shared.acknowledge_landed_processed()
                    if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                        shared.send_abort(f"DONE timeout at step {step_num}")
                        return
                    i += 1
                    continue

                tgt = dict(focus_target) if focus_target else None
                shared.send_move()
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    shared.send_abort(f"LANDED timeout enter step {step_num}")
                    return
                actual = self._capture()
                if tgt and not elements_match(tgt, actual):
                    healed = (
                        self._seek_tabs_forward_only(dict(tgt))
                        or self._seek_tabs_backward_then_forward(dict(tgt))
                    )
                    if not healed:
                        shared.send_abort(f"Enter destination not found at step {step_num}")
                        return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            shared.send_move()
            if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                shared.send_abort(f"DONE timeout at step {step_num}")
                return
            i += 1

    def _try_alternate_physical(self, alt_path: list[WorkflowStep]) -> bool:
        import pyautogui  # type: ignore

        ok = True
        for alt in alt_path:
            a_action = alt.get("action_type")
            if a_action == "press_tab":
                pyautogui.press("tab", presses=int(alt.get("tab_count", 1)), interval=AGAMI_POST_SETTLE)
                time.sleep(AGAMI_POST_SETTLE)
            elif a_action == "press_enter":
                if not (alt.get("is_final") or alt.get("is_destination")):
                    pyautogui.press("enter")
                    time.sleep(AGAMI_POST_SETTLE)
            elif a_action == "press_arrow":
                pyautogui.press(
                    alt["direction"],
                    presses=int(alt.get("count", 1)),
                    interval=AGAMI_ARROW_INTERVAL,
                )
                time.sleep(AGAMI_POST_SETTLE)
            elif a_action == "hotkey":
                pyautogui.hotkey(*alt.get("keys", []))
                time.sleep(AGAMI_POST_SETTLE)
            elif a_action == "press_escape":
                pyautogui.press("escape", presses=int(alt.get("count", 1)))
                time.sleep(AGAMI_ESCAPE_WAIT)
            elif a_action == "wait":
                time.sleep(float(alt.get("duration", 1.0)))
            elif a_action == "press_space":
                pyautogui.press("space", presses=int(alt.get("count", 1)))
                time.sleep(AGAMI_POST_SETTLE)

            a_anchor = alt.get("focus_target") or alt.get("intermediate_elements", [None])[0]
            if a_anchor and isinstance(a_anchor, dict):
                if not elements_match(dict(a_anchor), self._capture()):
                    ok = False
                    break
        return ok
