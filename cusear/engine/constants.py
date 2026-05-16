from __future__ import annotations

import os

# Rekky Detect: use CUSEAR_TIMING_PROFILE=rekky (or reliable) for step-by-step element capture.
# Override any value via env, e.g. REKKY_CAPTURE_WAIT=0.9 REKKY_BETWEEN_STEPS=1.0


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(float(raw)))
    except ValueError:
        return default


_TIMING_PROFILE = os.getenv("CUSEAR_TIMING_PROFILE", "rekky").strip().lower()
_RELIABLE = _TIMING_PROFILE not in ("fast", "speed")
# Rekky Detect: longest settles (Chrome focus, DOM, AppleScript/UIA read).
_REKKY_DETECT = _TIMING_PROFILE in ("rekky", "reliable", "accurate") or os.getenv(
    "REKKY_DETECT_ACCURATE", ""
).strip().lower() in ("1", "true", "yes")

# ── Rekky Detect / record (step-by-step capture) ─────────────────────────────
REKKY_TAB_INTERVAL = _env_float("REKKY_TAB_INTERVAL", 0.22 if _REKKY_DETECT else (0.12 if _RELIABLE else 0.10))
REKKY_TAB_POST_PRESS_SETTLE = _env_float("REKKY_TAB_POST_PRESS_SETTLE", 0.40 if _REKKY_DETECT else 0.25)
REKKY_ARROW_POST_PRESS_SETTLE = _env_float("REKKY_ARROW_POST_PRESS_SETTLE", 0.38 if _REKKY_DETECT else 0.28)
REKKY_CAPTURE_WAIT = _env_float("REKKY_CAPTURE_WAIT", 0.80 if _REKKY_DETECT else (0.55 if _RELIABLE else 0.30))
REKKY_DOM_SETTLE = _env_float("REKKY_DOM_SETTLE", 0.60 if _REKKY_DETECT else (0.40 if _RELIABLE else 0.25))
REKKY_POST_ACTION_SETTLE = _env_float("REKKY_POST_ACTION_SETTLE", 0.75 if _REKKY_DETECT else (0.50 if _RELIABLE else 0.35))
REKKY_PRE_CAPTURE_SETTLE = _env_float("REKKY_PRE_CAPTURE_SETTLE", 0.35 if _REKKY_DETECT else 0.25)
REKKY_BETWEEN_STEPS = _env_float("REKKY_BETWEEN_STEPS", 0.90 if _REKKY_DETECT else (0.65 if _RELIABLE else 0.45))
REKKY_OPEN_CHROME_SETTLE = _env_float("REKKY_OPEN_CHROME_SETTLE", 2.75 if _REKKY_DETECT else (2.00 if _RELIABLE else 1.50))
REKKY_ENTER_WAIT = _env_float("REKKY_ENTER_WAIT", 0.95 if _REKKY_DETECT else (0.55 if _RELIABLE else 0.50))
REKKY_POST_ENTER_SETTLE = _env_float("REKKY_POST_ENTER_SETTLE", 0.70 if _REKKY_DETECT else 0.50)
REKKY_ESCAPE_WAIT = _env_float("REKKY_ESCAPE_WAIT", 0.60 if _REKKY_DETECT else (0.45 if _RELIABLE else 0.40))
REKKY_ARROW_WAIT = _env_float("REKKY_ARROW_WAIT", 0.32 if _REKKY_DETECT else (0.22 if _RELIABLE else 0.20))
REKKY_URL_LOAD_WAIT = _env_float("REKKY_URL_LOAD_WAIT", 6.00 if _REKKY_DETECT else (4.50 if _RELIABLE else 4.00))
REKKY_TYPE_CLEAR_WAIT = _env_float("REKKY_TYPE_CLEAR_WAIT", 0.50 if _REKKY_DETECT else (0.35 if _RELIABLE else 0.30))
REKKY_AI_TYPE_SETTLE = _env_float("REKKY_AI_TYPE_SETTLE", 0.85 if _REKKY_DETECT else 0.55)
REKKY_CAPTURE_POLL_ATTEMPTS = _env_int("REKKY_CAPTURE_POLL_ATTEMPTS", 12 if _REKKY_DETECT else 6)
REKKY_CAPTURE_POLL_INTERVAL = _env_float("REKKY_CAPTURE_POLL_INTERVAL", 0.30 if _REKKY_DETECT else 0.20)
REKKY_PAGE_READY_TIMEOUT = _env_float("REKKY_PAGE_READY_TIMEOUT", 30.0 if _REKKY_DETECT else 18.0)
REKKY_PAGE_READY_POLL = _env_float("REKKY_PAGE_READY_POLL", 0.50 if _REKKY_DETECT else 0.35)
REKKY_CHROME_ACTIVATE_WAIT = _env_float("REKKY_CHROME_ACTIVATE_WAIT", 0.45 if _REKKY_DETECT else 0.30)

# ── Lucky dry-run validation ────────────────────────────────────────────────
LUCKY_TAB_CAPTURE_MODE = (
    os.getenv("LUCKY_TAB_CAPTURE_MODE", "all" if _RELIABLE else "smart").strip().lower() or "all"
)
LUCKY_TAB_INTERVAL = _env_float("LUCKY_TAB_INTERVAL", 0.10 if _RELIABLE else 0.08)
LUCKY_CAPTURE_WAIT = _env_float("LUCKY_CAPTURE_WAIT", 0.45 if _RELIABLE else 0.35)
LUCKY_POST_SETTLE = _env_float("LUCKY_POST_SETTLE", 0.45 if _RELIABLE else 0.40)
LUCKY_BETWEEN_STEPS = _env_float("LUCKY_BETWEEN_STEPS", 0.65 if _RELIABLE else 0.60)
LUCKY_REFRESH_WAIT = _env_float("LUCKY_REFRESH_WAIT", 3.50 if _RELIABLE else 3.00)
LUCKY_ENTER_WAIT = _env_float("LUCKY_ENTER_WAIT", 0.55 if _RELIABLE else 0.50)
LUCKY_ESCAPE_WAIT = _env_float("LUCKY_ESCAPE_WAIT", 0.45 if _RELIABLE else 0.40)
LUCKY_ARROW_WAIT = _env_float("LUCKY_ARROW_WAIT", 0.22 if _RELIABLE else 0.20)
LUCKY_TYPE_INTERVAL = _env_float("LUCKY_TYPE_INTERVAL", 0.05)
LUCKY_URL_LOAD_WAIT = _env_float("LUCKY_URL_LOAD_WAIT", 4.50 if _RELIABLE else 4.00)

# ── Agami walk / seek ─────────────────────────────────────────────────────────
AGAMI_TAB_INTERVAL = _env_float("AGAMI_TAB_INTERVAL", 0.08 if _RELIABLE else 0.06)
AGAMI_POST_SETTLE = _env_float("AGAMI_POST_SETTLE", 0.45 if _RELIABLE else 0.35)
AGAMI_BETWEEN_STEPS = _env_float("AGAMI_BETWEEN_STEPS", 0.55 if _RELIABLE else 0.50)
AGAMI_SEEK_WAIT = _env_float("AGAMI_SEEK_WAIT", 0.40 if _RELIABLE else 0.30)
AGAMI_ESCAPE_WAIT = _env_float("AGAMI_ESCAPE_WAIT", 0.45 if _RELIABLE else 0.40)
AGAMI_ARROW_INTERVAL = _env_float("AGAMI_ARROW_INTERVAL", 0.18 if _RELIABLE else 0.15)
AGAMI_URL_LOAD_WAIT = _env_float("AGAMI_URL_LOAD_WAIT", 4.50 if _RELIABLE else 4.00)

# ── AHA live execute (human pace; Agami gates each tab) ───────────────────────
AHA_MIN_GAP_SEC = _env_float("AHA_MIN_GAP_SEC", 1.00)
AHA_TAB_INTERVAL = _env_float("AHA_TAB_INTERVAL", 1.00)
AHA_POST_SETTLE = _env_float("AHA_POST_SETTLE", 1.00)
AHA_BETWEEN_STEPS = _env_float("AHA_BETWEEN_STEPS", 1.00)
AHA_DOM_SETTLE = _env_float("AHA_DOM_SETTLE", 0.35 if _RELIABLE else 0.25)
AHA_ESCAPE_WAIT = _env_float("AHA_ESCAPE_WAIT", 0.45 if _RELIABLE else 0.40)
AHA_ARROW_INTERVAL = _env_float("AHA_ARROW_INTERVAL", 1.00)
AHA_TYPE_INTERVAL = _env_float("AHA_TYPE_INTERVAL", 0.035)
AHA_URL_LOAD_WAIT = _env_float("AHA_URL_LOAD_WAIT", 4.50 if _RELIABLE else 4.00)

# ── Shared capture (macOS AppleScript → Chrome) ─────────────────────────────
DOM_SETTLE_BEFORE_CAPTURE = _env_float("DOM_SETTLE_BEFORE_CAPTURE", 0.38 if _REKKY_DETECT else (0.22 if _RELIABLE else 0.12))
CAPTURE_RETRY_INTERVAL = _env_float("CAPTURE_RETRY_INTERVAL", 0.32 if _REKKY_DETECT else (0.20 if _RELIABLE else 0.12))
CAPTURE_MAX_ATTEMPTS = _env_int("CAPTURE_MAX_ATTEMPTS", 10 if _REKKY_DETECT else (6 if _RELIABLE else 2))
APPLESCRIPT_WAIT = _env_float("APPLESCRIPT_WAIT", 0.32 if _REKKY_DETECT else (0.20 if _RELIABLE else 0.15))


def rekky_detect_timing_snapshot() -> dict[str, float | int | str]:
    """Logged at Detect start so timing issues can be diagnosed."""
    return {
        "profile": _TIMING_PROFILE,
        "rekky_detect_mode": _REKKY_DETECT,
        "tab_interval": REKKY_TAB_INTERVAL,
        "tab_post_settle": REKKY_TAB_POST_PRESS_SETTLE,
        "capture_wait": REKKY_CAPTURE_WAIT,
        "dom_settle": REKKY_DOM_SETTLE,
        "post_action_settle": REKKY_POST_ACTION_SETTLE,
        "between_steps": REKKY_BETWEEN_STEPS,
        "capture_poll_attempts": REKKY_CAPTURE_POLL_ATTEMPTS,
        "capture_max_attempts": CAPTURE_MAX_ATTEMPTS,
        "url_load_wait": REKKY_URL_LOAD_WAIT,
    }

URL_LOAD_WAIT = REKKY_URL_LOAD_WAIT
ESCAPE_WAIT = AHA_ESCAPE_WAIT
HOME_SETTLE = _env_float("HOME_SETTLE", 0.55 if _RELIABLE else 0.50)
SCREENSHOT_SETTLE = _env_float("SCREENSHOT_SETTLE", 0.35 if _RELIABLE else 0.30)
MOVE_TIMEOUT = _env_float("MOVE_TIMEOUT", 15.00)
DONE_TIMEOUT = _env_float("DONE_TIMEOUT", 30.00)
STEP_BRIDGE_TIMEOUT = _env_float("STEP_BRIDGE_TIMEOUT", 120.00)
LANDED_TIMEOUT = _env_float("LANDED_TIMEOUT", 12.00 if _RELIABLE else 10.00)

MAX_SEEK_FORWARD = 20
MAX_SEEK_BACKWARD = 5
MAX_SEEK_TABS = 20
