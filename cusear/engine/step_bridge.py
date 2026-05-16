from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Optional

from .constants import STEP_BRIDGE_TIMEOUT

_lock = threading.RLock()
_enabled = True
_aborted = False
_pending: dict[str, Any] | None = None
_ack_event = threading.Event()
_last_ack_payload: dict[str, Any] = {}
_progress_listener: Optional[Callable[[dict[str, Any]], None]] = None


def set_progress_listener(cb: Optional[Callable[[dict[str, Any]], None]]) -> None:
    global _progress_listener
    with _lock:
        _progress_listener = cb


def _notify_progress() -> None:
    cb = _progress_listener
    if not cb:
        return
    try:
        cb(pending_snapshot())
    except Exception:
        pass


class StepBridgeTimeout(Exception):
    pass


class StepBridgeAbort(Exception):
    pass


def set_enabled(enabled: bool) -> None:
    global _enabled
    with _lock:
        _enabled = bool(enabled)


def is_enabled() -> bool:
    with _lock:
        return _enabled


def reset_run() -> None:
    global _aborted, _pending, _last_ack_payload
    with _lock:
        _aborted = False
        _pending = None
        _last_ack_payload = {}
        _ack_event.clear()


def abort_run(reason: str = "") -> None:
    global _aborted
    with _lock:
        _aborted = True
        if _pending is not None:
            _pending["abort_reason"] = reason
        _ack_event.set()


def pending_snapshot() -> dict[str, Any]:
    with _lock:
        if not _pending:
            return {"pending": False}
        return {"pending": True, **dict(_pending)}


def announce(
    *,
    phase: str,
    bridge_phase: str,
    workflow_name: str,
    step_index: int,
    total_steps: int,
    step_number: int | None,
    action_type: str,
    extra: dict[str, Any] | None = None,
) -> str:
    global _pending
    token = uuid.uuid4().hex[:12]
    with _lock:
        if _aborted:
            raise StepBridgeAbort("run aborted")
        _pending = {
            "token": token,
            "phase": str(phase or "").strip().lower(),
            "bridge_phase": str(bridge_phase or "").strip().lower(),
            "workflow_name": str(workflow_name or "").strip(),
            "current_step": int(step_index),
            "total_steps": int(total_steps),
            "step_number": step_number,
            "action_type": str(action_type or "").strip(),
            "announced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **(extra or {}),
        }
        _ack_event.clear()
    _notify_progress()
    return token


def wait_for_ack_grace(
    token: str,
    *,
    grace_sec: float = 1.25,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Wait for Trainer ack; auto-continue after grace_sec if the UI did not respond."""
    if not is_enabled():
        return {}
    grace = max(0.0, float(grace_sec))
    fired = _ack_event.wait(timeout=grace)
    if not fired:
        acknowledge(token)
    return wait_for_ack(token, timeout=timeout)


def wait_for_ack(token: str, timeout: float | None = None) -> dict[str, Any]:
    global _last_ack_payload, _pending
    if not is_enabled():
        return {}
    limit = float(timeout if timeout is not None else STEP_BRIDGE_TIMEOUT)
    fired = _ack_event.wait(timeout=limit)
    with _lock:
        if _aborted:
            raise StepBridgeAbort(str((_pending or {}).get("abort_reason") or "run aborted"))
        if not fired:
            raise StepBridgeTimeout(
                f"Trainer app did not acknowledge {(_pending or {}).get('bridge_phase', 'step')} "
                f"on step {(_pending or {}).get('current_step', '?')} within {limit:.0f}s"
            )
        if not _pending or str(_pending.get("token") or "") != str(token):
            raise StepBridgeTimeout("step bridge token mismatch (stale ack)")
        payload = dict(_last_ack_payload)
        _last_ack_payload = {}
        _pending = None
        _ack_event.clear()
    return payload


def acknowledge(token: str, *, mapped_step: dict[str, Any] | None = None) -> bool:
    global _last_ack_payload, _pending
    with _lock:
        if not _pending or str(_pending.get("token") or "") != str(token or "").strip():
            return False
        _pending["acked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _last_ack_payload = {}
        if mapped_step:
            _last_ack_payload["mapped_step"] = dict(mapped_step)
        _ack_event.set()
        _notify_progress()
        return True


def gate_rekky_step(
    *,
    bridge_phase: str,
    workflow_name: str,
    step_index: int,
    total_steps: int,
    step_number: int | None,
    action_type: str,
    extra: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    if not is_enabled():
        return {}
    token = announce(
        phase="rekky",
        bridge_phase=bridge_phase,
        workflow_name=workflow_name,
        step_index=step_index,
        total_steps=total_steps,
        step_number=step_number,
        action_type=action_type,
        extra=extra,
    )
    return wait_for_ack(token, timeout=timeout)


def gate_before_step(
    *,
    phase: str,
    workflow_name: str,
    step_index: int,
    total_steps: int,
    step_number: int | None,
    action_type: str,
    extra: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> None:
    """AHA: single gate before executing a step."""
    if not is_enabled():
        return
    token = announce(
        phase=phase,
        bridge_phase="before_execute",
        workflow_name=workflow_name,
        step_index=step_index,
        total_steps=total_steps,
        step_number=step_number,
        action_type=action_type,
        extra=extra,
    )
    wait_for_ack(token, timeout=timeout)


def clear_pending() -> None:
    global _pending, _last_ack_payload
    with _lock:
        _pending = None
        _last_ack_payload = {}
        _ack_event.clear()
    _notify_progress()
