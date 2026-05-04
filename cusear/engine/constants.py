from __future__ import annotations

# Rekky enrichment — accurate, not fast
REKKY_TAB_INTERVAL = 0.10  # 100ms between tab presses
REKKY_CAPTURE_WAIT = 0.30  # 300ms after keypress before capturing element
REKKY_ENTER_WAIT = 0.50  # 500ms after enter press
REKKY_ESCAPE_WAIT = 0.40  # 400ms after escape press
REKKY_ARROW_WAIT = 0.20  # 200ms after arrow press
REKKY_URL_LOAD_WAIT = 4.00  # 4s after URL loads
REKKY_TYPE_CLEAR_WAIT = 0.30  # 300ms after clearing placeholder text

# Lucky — accuracy focused — slower
LUCKY_TAB_INTERVAL = 0.08  # 80ms between tabs
LUCKY_CAPTURE_WAIT = 0.35  # 350ms wait before capture validation
LUCKY_POST_SETTLE = 0.40  # fallback settle (unused for per-key Lucky tab path)
LUCKY_BETWEEN_STEPS = 0.60  # 600ms between steps
LUCKY_REFRESH_WAIT = 3.00  # 3s after page refresh
LUCKY_ENTER_WAIT = 0.50
LUCKY_ESCAPE_WAIT = 0.40
LUCKY_ARROW_WAIT = 0.20
LUCKY_TYPE_INTERVAL = 0.05  # 50ms per char for Lucky dry-run filler
LUCKY_URL_LOAD_WAIT = 4.00

# Agami — precise and purposeful
AGAMI_TAB_INTERVAL = 0.06  # 60ms between tabs (legacy batch / seekers)
AGAMI_POST_SETTLE = 0.35  # 350ms settle
AGAMI_BETWEEN_STEPS = 0.50  # 500ms between steps
AGAMI_SEEK_WAIT = 0.30  # 300ms after each seek tab / arrow
AGAMI_ESCAPE_WAIT = 0.40
AGAMI_ARROW_INTERVAL = 0.15
AGAMI_URL_LOAD_WAIT = 4.00

# AHA™ — fast and satisfying for user
AHA_TAB_INTERVAL = 0.05  # 50ms between tabs in a multi-press burst (expanded steps use count=1)
AHA_POST_SETTLE = 0.30
AHA_BETWEEN_STEPS = 0.50
AHA_ESCAPE_WAIT = 0.40
AHA_ARROW_INTERVAL = 0.12
AHA_TYPE_INTERVAL = 0.035  # 35ms per character
AHA_URL_LOAD_WAIT = 4.00

# Shared
URL_LOAD_WAIT = 4.00  # 4s after URL opens (backward compat)
ESCAPE_WAIT = 0.40  # backward compat (= AHA_ESCAPE_WAIT / REKKY_ESCAPE_WAIT baseline)
HOME_SETTLE = 0.50  # 500ms after Home key
APPLESCRIPT_WAIT = 0.15  # 150ms after AppleScript
SCREENSHOT_SETTLE = 0.30  # 300ms before screenshot
MOVE_TIMEOUT = 15.00
DONE_TIMEOUT = 30.00
LANDED_TIMEOUT = 10.00

MAX_SEEK_FORWARD = 20
MAX_SEEK_BACKWARD = 5
MAX_SEEK_TABS = 20  # backward compat (= MAX_SEEK_FORWARD)

