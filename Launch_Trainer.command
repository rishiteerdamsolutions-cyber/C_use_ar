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
echo "  Launching the Trainer Control Center (desktop window at /trainer)."
echo "  Public marketing site is separate: CUSEAR WEBSITE  UX UI → served at http://127.0.0.1:7788/ when dashboard runs."
echo ""

# Force desktop window mode unless user explicitly overrides.
export DESKTOP_FORCE_BROWSER=0
export TRAINER_NO_OPEN_BROWSER=1
export AGENCY_USER_MODE=trainer

# Ensure the desktop shell dependency exists (pywebview).
if ! "$PY_BIN" -c "import webview" >/dev/null 2>&1; then
  echo "  pywebview is missing — installing it now..."
  "$PY_BIN" -m pip install --user pywebview >/dev/null 2>&1 || true
fi

echo "  Starting… if the window doesn't appear in ~10s, check this terminal for errors."
echo ""
"$PY_BIN" desktop.py

echo
echo "Trainer exited."
echo "Press Enter to close."
read -r
