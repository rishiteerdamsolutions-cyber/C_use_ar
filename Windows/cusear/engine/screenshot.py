from __future__ import annotations

import os
import time
from typing import Any

from .constants import SCREENSHOT_SETTLE


def capture_screenshot(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    time.sleep(SCREENSHOT_SETTLE)
    import pyautogui  # type: ignore

    img: Any = pyautogui.screenshot()
    img.save(path)

