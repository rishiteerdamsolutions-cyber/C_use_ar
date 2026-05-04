from __future__ import annotations

import json
import logging
import platform as _platform
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .constants import APPLESCRIPT_WAIT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FocusElement:
    tagName: str = ""
    text: str = ""
    id: str = ""
    className: str = ""
    role: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "tagName": self.tagName,
            "text": self.text,
            "id": self.id,
            "className": self.className,
            "role": self.role,
        }


class OsAdapter(Protocol):
    def activate_chrome(self) -> None: ...
    def chrome_is_focused(self) -> bool: ...
    def safe_activate_chrome(self) -> None: ...
    def capture_active_element(self) -> dict[str, str]: ...
    def keep_display_awake(self, seconds: int = 1200) -> None: ...
    def stop_keep_awake(self) -> None: ...
    def close_chrome_windows(self) -> None: ...


_MAC_CHROME_ELEMENT_APPLESCRIPT = r"""
tell application "Google Chrome"
    if not (exists window 1) then return "{}"
    set jsCode to "(function() { var el = document.activeElement; if (!el || el === document.body) return JSON.stringify({tagName:'',text:'',id:'',className:'',role:''}); var text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().substring(0,100); return JSON.stringify({tagName: el.tagName.toLowerCase(), text: text, id: el.id || '', className: el.className || '', role: el.getAttribute('role') || ''}); })();"
    try
        set result to execute active tab of window 1 javascript jsCode
        return result
    on error
        return "{}"
    end try
end tell
"""


class MacOsAdapter:
    def activate_chrome(self) -> None:
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                timeout=3,
                capture_output=True,
            )
            time.sleep(0.3)
        except Exception:
            pass

    def chrome_is_focused(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to get name of first process whose frontmost is true',
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return "Chrome" in (result.stdout or "")
        except Exception:
            return True

    def safe_activate_chrome(self) -> None:
        if not self.chrome_is_focused():
            self.activate_chrome()
            time.sleep(0.3)

    def capture_active_element(self) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["osascript", "-e", _MAC_CHROME_ELEMENT_APPLESCRIPT],
                capture_output=True,
                text=True,
                timeout=4,
            )
            raw = (result.stdout or "").strip()
            time.sleep(APPLESCRIPT_WAIT)
            if not raw or raw == "{}":
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            out: dict[str, str] = {}
            for k in ("tagName", "text", "id", "className", "role"):
                v = data.get(k, "")
                out[k] = "" if v is None else str(v)
            return out
        except Exception:
            return {}

    def keep_display_awake(self, seconds: int = 1200) -> None:
        try:
            subprocess.Popen(["caffeinate", "-d", "-t", str(int(seconds))])
        except Exception as exc:
            logger.debug("keep_display_awake failed: %s", exc)

    def stop_keep_awake(self) -> None:
        try:
            subprocess.run(["pkill", "caffeinate"], capture_output=True, timeout=3)
        except Exception:
            pass

    def close_chrome_windows(self) -> None:
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Google Chrome" to close every window',
                ],
                timeout=3,
                capture_output=True,
            )
        except Exception:
            pass


class WindowsOsAdapter:
    def activate_chrome(self) -> None:
        # Best-effort: relies on pygetwindow if present.
        try:
            import pygetwindow as gw  # type: ignore

            wins = gw.getWindowsWithTitle("Chrome")
            if not wins:
                wins = gw.getWindowsWithTitle("Google Chrome")
            if wins:
                wins[0].activate()
                time.sleep(0.5)
        except Exception:
            pass

    def chrome_is_focused(self) -> bool:
        try:
            import win32gui  # type: ignore

            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            return "Chrome" in (title or "")
        except Exception:
            return True

    def safe_activate_chrome(self) -> None:
        if not self.chrome_is_focused():
            self.activate_chrome()
            time.sleep(0.3)

    def capture_active_element(self) -> dict[str, str]:
        """
        Windows-focused element capture.

        Uses UI Automation accessible focus. Requires either `uiautomation` or
        `pywinauto` to be installed. Returns the same JSON structure as macOS.
        """
        # Preferred: uiautomation
        try:
            import uiautomation as auto  # type: ignore

            ctrl = auto.GetFocusedControl()
            if not ctrl:
                return {}
            name = (ctrl.Name or "").strip()
            ctype = getattr(ctrl, "ControlTypeName", "") or ""
            role = ctype.lower()
            # We don't have DOM tagName on Windows; use role-ish mapping.
            tag = "div"
            if "edit" in role or "text" in role:
                tag = "input"
            if "button" in role:
                tag = "div"
                role = "button"
            return {
                "tagName": tag,
                "text": name[:100],
                "id": "",
                "className": "",
                "role": role[:50],
            }
        except Exception:
            pass

        # Fallback: pywinauto
        try:
            from pywinauto import Desktop  # type: ignore

            elem = Desktop(backend="uia").get_active()
            if not elem:
                return {}
            focused = elem.get_focus()
            name = (getattr(focused, "window_text", lambda: "")() or "").strip()
            return {
                "tagName": "div",
                "text": name[:100],
                "id": "",
                "className": "",
                "role": "",
            }
        except Exception:
            return {}

    def keep_display_awake(self, seconds: int = 1200) -> None:
        # Best-effort: prevent sleep while run is active.
        try:
            import ctypes  # noqa: S402

            ES_CONTINUOUS = 0x80000000
            ES_DISPLAY_REQUIRED = 0x00000002
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
        except Exception:
            pass

    def stop_keep_awake(self) -> None:
        try:
            import ctypes  # noqa: S402

            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass

    def close_chrome_windows(self) -> None:
        try:
            subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], timeout=5, capture_output=True)
        except Exception:
            pass


def open_url_in_google_chrome(url: str) -> None:
    """
    Open a URL in Google Chrome only.

    Do not use ``webbrowser.open()`` here: on macOS it follows the user's *default*
    browser (often Safari), which causes Safari + Chrome to both appear during WRA runs.
    """
    u = str(url or "").strip()
    if not u:
        return
    sys_name = _platform.system()
    if sys_name == "Darwin":
        subprocess.Popen(["open", "-a", "Google Chrome", u])
    elif sys_name == "Windows":
        subprocess.Popen(["cmd", "/c", "start", "", "chrome", u])
    else:
        subprocess.Popen(["google-chrome", u])


def get_os_adapter() -> OsAdapter:
    sys = _platform.system()
    if sys == "Darwin":
        return MacOsAdapter()
    if sys == "Windows":
        return WindowsOsAdapter()
    # Default: act like Windows for focus/close; element capture will be empty.
    return WindowsOsAdapter()

