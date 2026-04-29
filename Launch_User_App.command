#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer .venv so the same Python binary that gets permissions is used.
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

echo "Starting cusear™ User App using: $PY_BIN"
export AGENCY_USER_MODE=consumer
"$PY_BIN" desktop.py

echo
echo "User App exited."
echo "Press Enter to close."
read -r
