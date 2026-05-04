from __future__ import annotations

from .os_adapter import get_os_adapter


class FocusMode:
    def __init__(self) -> None:
        self._os = get_os_adapter()

    def enable(self, seconds: int = 1200) -> None:
        self._os.keep_display_awake(seconds=seconds)
        self._os.activate_chrome()

    def disable(self) -> None:
        self._os.stop_keep_awake()

