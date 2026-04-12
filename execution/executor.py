"""
Execution Engine — Autonomous Web Agency Agent v1.0
Physically drives mouse, keyboard, clipboard, and windows.
FAILSAFE always enabled: move mouse to top-left corner to abort.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ─── Safety constants ──────────────────────────────────────────────────────────
MIN_CONFIDENCE = 0.7
ACTION_PAUSE = 0.5          # seconds between every action
TYPING_INTERVAL = 0.04      # seconds between keystrokes

# ─── Custom Exceptions ─────────────────────────────────────────────────────────
class LowConfidenceError(Exception):
    """Raised when click confidence is below MIN_CONFIDENCE."""
    def __init__(self, confidence: float) -> None:
        super().__init__(
            f"Refusing to click — confidence {confidence:.2f} < {MIN_CONFIDENCE}"
        )


class WindowNotFoundError(Exception):
    """Raised when the target window cannot be located."""


class ExecutionEngineError(Exception):
    """General execution engine failure."""


# ─── Engine ────────────────────────────────────────────────────────────────────
class ExecutionEngine:
    """
    Safe desktop automation engine with pyautogui backend.

    All actions include:
    - Minimum pause between steps (ACTION_PAUSE)
    - Confidence check before every click
    - pyautogui FAILSAFE (top-left corner = emergency stop)
    - Structured logging
    """

    def __init__(self) -> None:
        try:
            import pyautogui                    # type: ignore
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = ACTION_PAUSE
            self._pg = pyautogui
            logger.info("ExecutionEngine: pyautogui ready, FAILSAFE=True")
        except ImportError as exc:
            raise ExecutionEngineError(
                "pyautogui not installed — run: pip install pyautogui"
            ) from exc

    @staticmethod
    def _primary_mod() -> str:
        """command on macOS, ctrl on Windows/Linux."""
        import platform

        return "command" if platform.system() == "Darwin" else "ctrl"

    # ── Click ──────────────────────────────────────────────────────────────────
    def click(self, x: int, y: int, confidence: float) -> None:
        """
        Left-click at (x, y).

        Args:
            x, y:       Screen coordinates.
            confidence: Must be >= MIN_CONFIDENCE or click is refused.

        Raises:
            LowConfidenceError: Confidence too low.
        """
        if confidence < MIN_CONFIDENCE:
            logger.error("Click refused at (%d,%d) confidence=%.2f", x, y, confidence)
            raise LowConfidenceError(confidence)

        logger.info("Click (%d, %d) confidence=%.2f", x, y, confidence)
        self._pg.click(x, y)
        time.sleep(ACTION_PAUSE)

    def double_click(self, x: int, y: int, confidence: float) -> None:
        """Double-click at (x, y)."""
        if confidence < MIN_CONFIDENCE:
            raise LowConfidenceError(confidence)
        logger.info("Double-click (%d, %d)", x, y)
        self._pg.doubleClick(x, y)
        time.sleep(ACTION_PAUSE)

    def right_click(self, x: int, y: int, confidence: float) -> None:
        """Right-click at (x, y)."""
        if confidence < MIN_CONFIDENCE:
            raise LowConfidenceError(confidence)
        logger.info("Right-click (%d, %d)", x, y)
        self._pg.rightClick(x, y)
        time.sleep(ACTION_PAUSE)

    # ── Typing ─────────────────────────────────────────────────────────────────
    def type_text(self, text: str, clear_first: bool = True) -> None:
        """
        Type text into the currently focused field.

        Args:
            text:        String to type.
            clear_first: Select-all + delete before typing (default True).
        """
        if clear_first:
            self._pg.hotkey(self._primary_mod(), "a")
            time.sleep(0.1)
            self._pg.press("delete")
            time.sleep(0.1)

        logger.info("Typing %d characters", len(text))
        self._pg.write(text, interval=TYPING_INTERVAL)
        time.sleep(ACTION_PAUSE)

    def type_slow(self, text: str, interval: float = 0.08) -> None:
        """Type with a slower interval (useful for terminal / Cursor AI)."""
        logger.info("Slow-typing %d characters", len(text))
        self._pg.write(text, interval=interval)
        time.sleep(ACTION_PAUSE)

    # ── Clipboard ──────────────────────────────────────────────────────────────
    def copy_all_text(self) -> str:
        """
        Select all and copy, return clipboard content.

        Returns:
            String content from clipboard.
        """
        import pyperclip                        # type: ignore

        mod = self._primary_mod()
        self._pg.hotkey(mod, "a")
        time.sleep(0.2)
        self._pg.hotkey(mod, "c")
        time.sleep(0.3)

        text: str = pyperclip.paste()
        logger.info("Copied %d characters from clipboard", len(text))
        return text

    def copy_selection(self) -> None:
        """Copy the current selection to the clipboard (⌘C / Ctrl+C). Selection must already be active."""
        mod = self._primary_mod()
        logger.info("Copy selection (%s+C)", mod)
        self._pg.hotkey(mod, "c")
        time.sleep(ACTION_PAUSE)

    def paste(self) -> None:
        """Paste clipboard content into the current focus (⌘V / Ctrl+V)."""
        mod = self._primary_mod()
        logger.info("Paste (%s+V)", mod)
        self._pg.hotkey(mod, "v")
        time.sleep(ACTION_PAUSE)

    def set_clipboard(self, text: str) -> None:
        """Write text to clipboard without typing it."""
        import pyperclip                        # type: ignore
        pyperclip.copy(text)
        logger.debug("Clipboard set (%d chars)", len(text))

    # ── Windows ────────────────────────────────────────────────────────────────
    def switch_window(self, keyword: str) -> bool:
        """
        Bring the first window whose title contains `keyword` to the foreground.

        Args:
            keyword: Case-insensitive window title substring.

        Returns:
            True if window found and activated, False otherwise.
        """
        try:
            import pygetwindow as gw            # type: ignore
            windows = gw.getWindowsWithTitle(keyword)
            if not windows:
                # try case-insensitive search
                all_wins = gw.getAllWindows()
                windows = [w for w in all_wins if keyword.lower() in w.title.lower()]

            if windows:
                win = windows[0]
                win.activate()
                time.sleep(0.8)
                logger.info("Switched to window: '%s'", win.title)
                return True

            logger.warning("Window not found for keyword: '%s'", keyword)
            return False

        except ImportError:
            logger.warning("pygetwindow not installed — switch_window unavailable")
            return False
        except Exception as exc:
            logger.error("switch_window error: %s", exc)
            return False

    # ── Navigation ─────────────────────────────────────────────────────────────
    def open_url(self, url: str) -> None:
        """
        Open a URL in the default browser.

        Args:
            url: Full URL including scheme.
        """
        import webbrowser
        logger.info("Opening URL: %s", url)
        webbrowser.open(url)
        time.sleep(2)  # give browser time to open

    # ── Scrolling ──────────────────────────────────────────────────────────────
    def scroll_down(self, amount: int = 3) -> None:
        """Scroll down by `amount` wheel ticks."""
        logger.debug("Scroll down %d", amount)
        self._pg.scroll(-amount)
        time.sleep(ACTION_PAUSE)

    def scroll_up(self, amount: int = 3) -> None:
        """Scroll up by `amount` wheel ticks."""
        logger.debug("Scroll up %d", amount)
        self._pg.scroll(amount)
        time.sleep(ACTION_PAUSE)

    # ── Keys ───────────────────────────────────────────────────────────────────
    def press_enter(self) -> None:
        """Press the Enter key."""
        logger.debug("Press ENTER")
        self._pg.press("enter")
        time.sleep(ACTION_PAUSE)

    def press_escape(self) -> None:
        """Press the Escape key."""
        logger.debug("Press ESCAPE")
        self._pg.press("escape")
        time.sleep(ACTION_PAUSE)

    def shortcut(self, *keys: str) -> None:
        """
        Press a keyboard shortcut.

        Args:
            *keys: Keys in order, e.g. shortcut('ctrl', 'shift', 'p').
        """
        logger.info("Shortcut: %s", "+".join(keys))
        self._pg.hotkey(*keys)
        time.sleep(ACTION_PAUSE)

    # ── Utility ────────────────────────────────────────────────────────────────
    def move_to(self, x: int, y: int, duration: float = 0.3) -> None:
        """Move mouse without clicking."""
        self._pg.moveTo(x, y, duration=duration)

    def get_screen_size(self) -> tuple[int, int]:
        """Return (width, height) of the primary screen."""
        return self._pg.size()

    def wait(self, seconds: float) -> None:
        """Explicit wait (use sparingly)."""
        logger.debug("Waiting %.1f s", seconds)
        time.sleep(seconds)
