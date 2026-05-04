from __future__ import annotations

import threading


class SharedState:
    """
    Coordination between Agami and AHA™.

    MOVE       : Agami -> AHA™ (execute the next atomic key/UI action now)
    LANDED_N   : AHA™  -> Agami (Atomic key landed at position N; read active element now)
    DONE       : AHA™  -> Agami (Current session step payload finished)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._move_event = threading.Event()
        self._done_event = threading.Event()
        self._landed_event = threading.Event()
        self._landed_processed_event = threading.Event()
        self._landed_position = 0
        self.abort = False
        self.abort_reason = ""

    def send_move(self) -> None:
        self._move_event.set()

    def wait_for_move(self, timeout: float = 15.0) -> str:
        fired = self._move_event.wait(timeout=timeout)
        self._move_event.clear()
        if not fired:
            return "TIMEOUT"
        if self.abort:
            return "ABORT"
        return "MOVE"

    def send_landed_at(self, position: int) -> None:
        with self._lock:
            self._landed_position = position
        self._landed_event.set()

    def wait_for_landed(self, timeout: float = 10.0) -> tuple[bool, int]:
        fired = self._landed_event.wait(timeout=timeout)
        self._landed_event.clear()
        with self._lock:
            position = self._landed_position
        return fired, position

    def acknowledge_landed_processed(self) -> None:
        """
        Agami acknowledges it has validated/healed after each LANDED_N so AHA can finish
        the current session-step (DONE) without racing ahead of inserts.
        """
        self._landed_processed_event.set()

    def wait_landed_processed(self, timeout: float = 15.0) -> bool:
        fired = self._landed_processed_event.wait(timeout=timeout)
        self._landed_processed_event.clear()
        return fired

    def send_done(self) -> None:
        self._done_event.set()

    def wait_for_done(self, timeout: float = 30.0) -> bool:
        fired = self._done_event.wait(timeout=timeout)
        self._done_event.clear()
        return fired

    def send_abort(self, reason: str = "") -> None:
        with self._lock:
            self.abort = True
            self.abort_reason = reason
        self._move_event.set()
        self._done_event.set()
        self._landed_event.set()
        self._landed_processed_event.set()

    def reset(self) -> None:
        with self._lock:
            self.abort = False
            self.abort_reason = ""
            self._landed_position = 0
        self._move_event.clear()
        self._done_event.clear()
        self._landed_event.clear()
        self._landed_processed_event.clear()
