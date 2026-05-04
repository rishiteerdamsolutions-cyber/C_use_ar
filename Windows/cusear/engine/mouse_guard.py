from __future__ import annotations

import threading
import time

from .os_adapter import get_os_adapter


class MouseGuard:
    """
    Watches for mouse movement and silently restores Chrome focus.
    Never moves the mouse; never blocks mouse usage.
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_pos: tuple[int, int] | None = None
        self._os = get_os_adapter()

    def start(self) -> None:
        import pyautogui  # type: ignore

        self._last_pos = tuple(pyautogui.position())
        self._running = True
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _watch(self) -> None:
        import pyautogui  # type: ignore

        while self._running:
            try:
                current = tuple(pyautogui.position())
                if self._last_pos is not None and current != self._last_pos:
                    self._os.activate_chrome()
                    self._last_pos = current
            except Exception:
                pass
            time.sleep(0.1)

