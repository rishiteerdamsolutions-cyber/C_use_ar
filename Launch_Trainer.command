#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer .venv so the same Python binary that gets Accessibility permission is used.
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
  PY_BIN="$SCRIPT_DIR/.venv/bin/python3"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PY_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
else
  echo "Python is not installed. Install Python 3 first."
  echo "Press Enter to close."
  read -r
  exit 1
fi

echo "Starting cusear™ Trainer using: $PY_BIN"
echo ""
echo "  This runs desktop.py: local server on port 7788, then opens the Trainer in an app window (pywebview)."
echo "  That window is still a web view (like Safari) — not a separate native UI toolkit."
echo "  If anything feels stuck: click the Trainer window once (focus), or run with  export DESKTOP_WEBVIEW_DEBUG=1"
echo "  then right-click → Inspect Element to see console errors."
echo "  Prefer the system browser instead:  export DESKTOP_FORCE_BROWSER=1  before this script."
echo "  Wait for a green dot and “Server online” (or tap ↻ Retry in the header)."
echo "  If nothing loads: run  bash START.sh  in this folder (same server, your browser)."
echo ""
echo "  Use the top bar → 📦 Export app  (or ar™ tab → Desktop app export) to build Mac/Windows packages."
export AGENCY_USER_MODE=trainer
"$PY_BIN" desktop.py

echo
echo "Trainer exited."
echo "Press Enter to close."
read -r
