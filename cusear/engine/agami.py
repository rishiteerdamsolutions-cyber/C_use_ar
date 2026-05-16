from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

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

# LinkedIn: expect at most one extra tab-stop (e.g. banner) before Post.
LINKEDIN_PREFINAL_TAB_MAX_PROBE = 1


def linkedin_probe_extra_tab_count_to_target(
    *,
    capture: Callable[[], Any],
    tgt: dict[str, Any],
    max_probe: int,
    press_tab: Callable[[], None],
    press_shift_tab: Callable[[], None],
    sleep_after_motion: Callable[[], None],
) -> int:
    """
    Return k (1..max_probe) if k Tab presses reach `tgt`, after restoring focus with Shift+Tab.
    Return 0 if already on target or no match within max_probe (focus restored).
    """
    if elements_match(dict(tgt), capture() or {}):
        return 0
    for k in range(1, max_probe + 1):
        press_tab()
        sleep_after_motion()
        if elements_match(dict(tgt), capture() or {}):
            for _ in range(k):
                press_shift_tab()
                sleep_after_motion()
            return k
    for _ in range(max_probe):
        press_shift_tab()
        sleep_after_motion()
    return 0


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
        workflow_platform: str | None = None,
    ) -> None:
        self._os = get_os_adapter()
        self._company_endpoint = company_endpoint
        self._company_logs_dir = company_logs_dir
        self._screenshots_dir = screenshots_dir
        self._workflow_platform = (str(workflow_platform).strip().lower() if workflow_platform else "") or ""
        self._action_logger = self._setup_action_logger()

    def _setup_action_logger(self) -> logging.Logger:
        import os

        logs_dir = os.path.join(os.path.dirname(self._company_logs_dir), "agami")
        os.makedirs(logs_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(logs_dir, f"agami_{stamp}.log")
        name = f"cusear.agami.actions.{stamp}.{id(self)}"
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d [AGAMI] ACTION=%(message)s",
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

    def opening_sequence(self) -> None:
        import pyautogui  # type: ignore

        self._os.safe_activate_chrome()
        pyautogui.press("home")
        time.sleep(HOME_SETTLE)
        self._os.safe_activate_chrome()

    def _capture(self) -> dict[str, str]:
        return self._os.capture_active_element()

    def _is_aha_execute_only(self, step: WorkflowStep) -> bool:
        return bool(step.get("aha_execute_only") or step.get("execute_only_in_aha"))

    def _alt_rank_path(self) -> str:
        return f"{self._company_logs_dir}/agami_alternate_rankings.json"

    def _load_alt_rankings(self) -> dict[str, int]:
        path = self._alt_rank_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_alt_rankings(self, rankings: dict[str, int]) -> None:
        try:
            with open(self._alt_rank_path(), "w", encoding="utf-8") as f:
                json.dump(rankings, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _alt_signature(self, alt_path: list[WorkflowStep]) -> str:
        tokens: list[str] = []
        for st in alt_path[:8]:
            if not isinstance(st, dict):
                continue
            act = str(st.get("action_type") or "")
            tokens.append(act)
            if act == "press_tab":
                tokens.append(f"t{int(st.get('tab_count', 1))}")
            if act == "press_arrow":
                tokens.append(f"a{str(st.get('direction', 'down'))}:{int(st.get('count', 1))}")
        return "|".join(tokens) or "empty_alt"

    def _rank_alternates(self, alt_list: list[Any]) -> list[list[WorkflowStep]]:
        ranked: list[list[WorkflowStep]] = []
        rankings = self._load_alt_rankings()
        if alt_list and all(isinstance(x, dict) for x in alt_list):
            ranked = [alt_list]  # legacy shape: one full alternate path
            ranked.sort(key=lambda x: rankings.get(self._alt_signature(x), 0), reverse=True)
            return ranked
        for item in alt_list:
            if isinstance(item, list):
                ranked.append(item)
            elif isinstance(item, dict):
                ranked.append([item])
        ranked.sort(key=lambda x: rankings.get(self._alt_signature(x), 0), reverse=True)
        return ranked

    def _record_alternate_result(self, alt_path: list[WorkflowStep], *, success: bool) -> None:
        rankings = self._load_alt_rankings()
        key = self._alt_signature(alt_path)
        current = int(rankings.get(key, 0))
        rankings[key] = current + 1 if success else max(-10, current - 1)
        self._save_alt_rankings(rankings)

    def _healer_limits(self, step: WorkflowStep) -> tuple[int, int]:
        """
        Per-platform seek budgets. Platform can be supplied in workflow step via:
        - platform_hint: facebook|instagram|linkedin|x|whatsapp
        """
        hint = str(step.get("platform_hint") or "").strip().lower()
        if hint in {"instagram", "x", "twitter"}:
            return max(MAX_SEEK_FORWARD, 16), max(MAX_SEEK_BACKWARD, 10)
        if hint in {"facebook", "whatsapp"}:
            return max(MAX_SEEK_FORWARD, 12), max(MAX_SEEK_BACKWARD, 8)
        if hint in {"linkedin"}:
            return max(MAX_SEEK_FORWARD, 10), max(MAX_SEEK_BACKWARD, 6)
        return MAX_SEEK_FORWARD, MAX_SEEK_BACKWARD

    def _is_linkedin_heal_context(self, step: WorkflowStep) -> bool:
        if self._workflow_platform == "linkedin":
            return True
        return str(step.get("platform_hint") or "").strip().lower() == "linkedin"

    def _linkedin_extra_tabs_before_post(
        self, tgt: dict[str, Any], *, max_probe: int = LINKEDIN_PREFINAL_TAB_MAX_PROBE
    ) -> int:
        """
        LinkedIn-only: detect an extra tab-stop (e.g. promo banner) before Post.

        Returns how many Tab presses are needed to reach `tgt`, probing with Tab then
        restoring focus with Shift+Tab so AHA can replay the inserted press_tab steps.
        """
        import pyautogui  # type: ignore

        return linkedin_probe_extra_tab_count_to_target(
            capture=self._capture,
            tgt=dict(tgt),
            max_probe=max_probe,
            press_tab=lambda: pyautogui.press("tab"),
            press_shift_tab=lambda: pyautogui.hotkey("shift", "tab"),
            sleep_after_motion=lambda: time.sleep(AGAMI_SEEK_WAIT),
        )

    def _seek_tabs_forward_only(self, expected: dict[str, Any], *, max_forward: int = MAX_SEEK_FORWARD) -> bool:
        import pyautogui  # type: ignore

        for _fwd in range(1, max_forward + 1):
            self._alog(f"SEEK_FORWARD attempt={_fwd} expected={expected}", "press_tab")
            pyautogui.press("tab")
            time.sleep(AGAMI_SEEK_WAIT)
            if elements_match(expected, self._capture()):
                logger.info("[Agami] Found forward tab +%s", _fwd)
                self._alog(f"SEEK_FORWARD attempt={_fwd} expected={expected}", "matched")
                return True
        self._alog(f"SEEK_FORWARD expected={expected}", "not_found")
        return False

    def _seek_tabs_backward_then_forward(
        self,
        expected: dict[str, Any],
        *,
        max_forward: int = MAX_SEEK_FORWARD,
        max_backward: int = MAX_SEEK_BACKWARD,
    ) -> bool:
        import pyautogui  # type: ignore

        restore = max_forward + max_backward
        for _ in range(restore):
            pyautogui.hotkey("shift", "tab")
            time.sleep(AGAMI_SEEK_WAIT)
        for _bk in range(1, max_backward + 1):
            self._alog(f"SEEK_BACKWARD_WINDOW attempt={_bk} expected={expected}", "checking")
            if elements_match(expected, self._capture()):
                logger.info("[Agami] Found backward window near -%s", _bk)
                self._alog(f"SEEK_BACKWARD_WINDOW attempt={_bk} expected={expected}", "matched")
                return True
            pyautogui.press("tab")
            time.sleep(AGAMI_SEEK_WAIT)
        self._alog(f"SEEK_BACKWARD_WINDOW expected={expected}", "not_found")
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

        def _send_move(step_num: int, action: str) -> None:
            self._alog(f"MOVE_SEND step={step_num} action={action}", "sent")
            shared.send_move()

        def _send_abort(reason: str) -> None:
            self._alog("ABORT_SEND", reason)
            shared.send_abort(reason)

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
            aha_only = self._is_aha_execute_only(step)

            if step.get("is_checkpoint"):
                last_checkpoint_index = i

            if aha_only:
                # Observe-only in Agami: AHA executes the step; Agami keeps the handshake healthy.
                _send_move(step_num, action)
                landed_actions = {
                    "press_tab",
                    "press_enter",
                    "press_arrow",
                    "press_escape",
                    "press_space",
                    "press_home",
                    "press_end",
                    "hotkey",
                }
                if action in landed_actions:
                    fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                    if not fired:
                        _send_abort(f"LANDED timeout (AHA-only) at step {step_num}")
                        return
                    shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout (AHA-only) at step {step_num}")
                    return
                i += 1
                continue

            if action in ["open_url", "wait", "maximise_window", "close_browser", "type_text", "ai_type"]:
                _send_move(step_num, action)
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_tab":
                max_forward, max_backward = self._healer_limits(step)
                expected = dict(intermediates[0]) if intermediates else (anchor if anchor else None)
                _send_move(step_num, action)
                fired, _pos = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout at step {step_num}")
                    return
                actual = self._capture()

                healed = False
                if expected and not elements_match(expected, actual):
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "mismatch",
                    )
                    logger.warning("[Agami] Drift tab step %s", step_num)
                    session.insert_extra_tab_after(i)
                    self._alog(f"SESSION_INSERT_EXTRA_TAB step={step_num} index={i}", "inserted")
                    healed = self._seek_tabs_forward_only(dict(expected), max_forward=max_forward)

                    alt_path = step.get("alternate_path")
                    alt_list = alt_path if isinstance(alt_path, list) else []

                    if not healed:
                        healed = self._seek_tabs_backward_then_forward(
                            dict(expected), max_forward=max_forward, max_backward=max_backward
                        )

                    if not healed and alt_list:
                        for alt_candidate in self._rank_alternates(alt_list):
                            if self._try_alternate_physical(alt_candidate):
                                session.replace_step_with_alternate(i + 1, alt_candidate)
                                self._record_alternate_result(alt_candidate, success=True)
                                healed = True
                                break
                            self._record_alternate_result(alt_candidate, success=False)

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
                        _send_abort(f"Element not found at step {step_num}")
                        return
                elif expected:
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "matched",
                    )

                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_arrow":
                direction = str(step.get("direction", "down"))
                expected = dict(intermediates[0]) if intermediates else (anchor if anchor else None)
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout arrow step {step_num}")
                    return
                actual = self._capture()

                healed = False
                if expected and not elements_match(dict(expected), actual):
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "mismatch",
                    )
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
                    _send_abort(f"Arrow destination not found at step {step_num}")
                    return
                elif expected:
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "matched",
                    )

                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_escape":
                max_forward, max_backward = self._healer_limits(step)
                expected = dict(intermediates[0]) if intermediates else None
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout escape step {step_num}")
                    return
                actual = self._capture()
                if expected and not elements_match(expected, actual):
                    if not (
                        self._seek_tabs_forward_only(expected)
                        or self._seek_tabs_backward_then_forward(
                            expected, max_forward=max_forward, max_backward=max_backward
                        )
                    ):
                        shared.send_abort(f"Escape focus mismatch step {step_num}")
                        self._alog("ABORT_SEND", f"Escape focus mismatch step {step_num}")
                        return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_space":
                expected = dict(intermediates[0]) if intermediates else None
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout space step {step_num}")
                    return
                actual = self._capture()
                if expected and not elements_match(expected, actual):
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "mismatch",
                    )
                    _send_abort(f"Space landing mismatch step {step_num}")
                    return
                elif expected:
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={expected} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "matched",
                    )
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    shared.send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_home":
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout home step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_end":
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout end step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "hotkey":
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout hotkey step {step_num}")
                    return
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            if action == "press_enter":
                if step.get("is_final") or step.get("is_destination"):
                    tgt = dict(focus_target) if focus_target else None
                    if tgt:
                        actual = self._capture()
                        if not elements_match(tgt, actual):
                            extra_tabs = 0
                            if self._is_linkedin_heal_context(step):
                                extra_tabs = self._linkedin_extra_tabs_before_post(dict(tgt))
                            if extra_tabs > 0:
                                for j in range(extra_tabs):
                                    session.insert_extra_tab_at(i + j)
                                self._alog(
                                    f"SESSION_INSERT_LINKEDIN_PREFINAL_TAB count={extra_tabs} at_index={i} step={step_num}",
                                    "inserted",
                                )
                                send_to_company(
                                    company_endpoint=self._company_endpoint,
                                    logs_dir=self._company_logs_dir,
                                    trigger="agami_linkedin_banner_tab_heal",
                                    step=step_num,
                                    expected=tgt,
                                    actual=actual,
                                    extra_notes=f"Inserted {extra_tabs} press_tab before final enter",
                                )
                                continue
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
                            self._alog(
                                f"ELEMENT_CHECK step={step_num} action={action} expected={tgt} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                                "mismatch",
                            )
                            _send_abort("Final destination element mismatch")
                            return
                        self._alog(
                            f"ELEMENT_CHECK step={step_num} action={action} expected={tgt} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                            "matched",
                        )
                    _send_move(step_num, action)
                    fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                    if not fired:
                        _send_abort(f"LANDED timeout final enter step {step_num}")
                        return
                    shared.acknowledge_landed_processed()
                    if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                        _send_abort(f"DONE timeout at step {step_num}")
                        return
                    i += 1
                    continue

                tgt = dict(focus_target) if focus_target else None
                max_forward, max_backward = self._healer_limits(step)
                _send_move(step_num, action)
                fired, _ = shared.wait_for_landed(timeout=LANDED_TIMEOUT)
                if not fired:
                    _send_abort(f"LANDED timeout enter step {step_num}")
                    return
                actual = self._capture()
                if tgt and not elements_match(tgt, actual):
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={tgt} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "mismatch",
                    )
                    healed = (
                        self._seek_tabs_forward_only(dict(tgt), max_forward=max_forward)
                        or self._seek_tabs_backward_then_forward(
                            dict(tgt), max_forward=max_forward, max_backward=max_backward
                        )
                    )
                    if not healed:
                        _send_abort(f"Enter destination not found at step {step_num}")
                        return
                elif tgt:
                    self._alog(
                        f"ELEMENT_CHECK step={step_num} action={action} expected={tgt} actual_tag={actual.get('tagName')} actual_text={actual.get('text')} actual_role={actual.get('role')}",
                        "matched",
                    )
                shared.acknowledge_landed_processed()
                if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                    _send_abort(f"DONE timeout at step {step_num}")
                    return
                i += 1
                continue

            _send_move(step_num, action)
            if not shared.wait_for_done(timeout=DONE_TIMEOUT):
                _send_abort(f"DONE timeout at step {step_num}")
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
