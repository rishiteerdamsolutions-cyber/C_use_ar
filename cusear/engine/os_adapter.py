from __future__ import annotations

import json
import logging
import platform as _platform
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .constants import (
    APPLESCRIPT_WAIT,
    CAPTURE_MAX_ATTEMPTS,
    CAPTURE_RETRY_INTERVAL,
    DOM_SETTLE_BEFORE_CAPTURE,
)

logger = logging.getLogger(__name__)


def element_capture_signal(el: dict[str, str] | None) -> bool:
    if not el:
        return False
    if str(el.get("id") or "").strip():
        return True
    if str(el.get("tagName") or "").strip():
        return True
    if str(el.get("role") or "").strip():
        return True
    return len(str(el.get("text") or "").strip()) >= 2


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
    def wait_for_page_ready(self, timeout_sec: float = 18.0, poll_sec: float = 0.35) -> bool: ...
    def keep_display_awake(self, seconds: int = 1200) -> None: ...
    def stop_keep_awake(self) -> None: ...
    def close_chrome_windows(self) -> None: ...


_MAC_CHROME_ELEMENT_APPLESCRIPT = r"""
tell application "Google Chrome"
    if not (exists window 1) then return "{}"
    set jsCode to "(function(){function sig(e){if(!e||e===document.body)return false;var t=(e.innerText||e.value||e.getAttribute('aria-label')||'').trim();var tag=(e.tagName||'').toUpperCase();return!!(e.id||e.getAttribute('role')||t.length>=2||/^(BUTTON|A|INPUT|TEXTAREA|SELECT)$/.test(tag));}var el=document.activeElement;if(!el||el===document.body){el=document.querySelector('[contenteditable=true]:focus')||document.querySelector(':focus')||document.body;}var cur=el;for(var i=0;i<10&&cur&&cur!==document.body;i++){if(sig(cur)){el=cur;break;}cur=cur.parentElement;}if(!sig(el))return JSON.stringify({tagName:'',text:'',id:'',className:'',role:''});var text=(el.innerText||el.value||el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').trim().substring(0,100);return JSON.stringify({tagName:(el.tagName||'').toLowerCase(),text:text,id:el.id||'',className:typeof el.className==='string'?el.className:'',role:el.getAttribute('role')||''});})();"
    try
        set result to execute active tab of window 1 javascript jsCode
        return result
    on error
        return "{}"
    end try
end tell
"""

_MAC_CHROME_READY_APPLESCRIPT = r"""
tell application "Google Chrome"
    if not (exists window 1) then return "loading"
    set jsCode to "document.readyState||'loading'"
    try
        return execute active tab of window 1 javascript jsCode
    on error
        return "loading"
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

    def _capture_active_element_once(self) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["osascript", "-e", _MAC_CHROME_ELEMENT_APPLESCRIPT],
                capture_output=True,
                text=True,
                timeout=6,
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

    def capture_active_element(self) -> dict[str, str]:
        self.safe_activate_chrome()
        time.sleep(DOM_SETTLE_BEFORE_CAPTURE)
        last: dict[str, str] = {}
        attempts = max(1, int(CAPTURE_MAX_ATTEMPTS))
        for attempt in range(attempts):
            last = self._capture_active_element_once()
            if element_capture_signal(last):
                return last
            if attempt + 1 < attempts:
                time.sleep(CAPTURE_RETRY_INTERVAL)
        return last

    def wait_for_page_ready(self, timeout_sec: float = 18.0, poll_sec: float = 0.35) -> bool:
        deadline = time.time() + max(1.0, float(timeout_sec))
        while time.time() < deadline:
            try:
                result = subprocess.run(
                    ["osascript", "-e", _MAC_CHROME_READY_APPLESCRIPT],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                state = (result.stdout or "").strip().lower()
                if state in ("complete", "interactive"):
                    return True
            except Exception:
                pass
            time.sleep(max(0.1, float(poll_sec)))
        return False

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
        """Windows capture with the same retry/settle policy as macOS."""
        self.safe_activate_chrome()
        time.sleep(DOM_SETTLE_BEFORE_CAPTURE)
        last = self._capture_active_element_windows_once()
        attempts = max(1, int(CAPTURE_MAX_ATTEMPTS))
        for attempt in range(attempts):
            last = self._capture_active_element_windows_once()
            if element_capture_signal(last):
                return last
            if attempt + 1 < attempts:
                time.sleep(CAPTURE_RETRY_INTERVAL)
        return last

    def wait_for_page_ready(self, timeout_sec: float = 18.0, poll_sec: float = 0.35) -> bool:
        _ = timeout_sec, poll_sec
        time.sleep(1.0)
        return True

    def _capture_active_element_windows_once(self) -> dict[str, str]:
        try:
            import uiautomation as auto  # type: ignore

            ctrl = auto.GetFocusedControl()
            if not ctrl:
                return {}
            name = (ctrl.Name or "").strip()
            ctype = getattr(ctrl, "ControlTypeName", "") or ""
            role = ctype.lower()
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

