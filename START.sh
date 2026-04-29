#!/usr/bin/env bash
# ── cusear™ — Local Startup ──────────────────────────────────────────────────
# Creates .venv in this repo, installs requirements (including pyautogui), and
# starts the Trainer dashboard with that interpreter.
#
# Usage:
#   bash START.sh
# ────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ⚡ cusear™"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! command -v python3 &>/dev/null; then
  echo "  ✗ Python 3 not found."
  echo "  Install from https://python.org/downloads"
  exit 1
fi

echo "  ✓ Host Python $(python3 --version 2>&1 | cut -d' ' -f2)"

if [ ! -d "$ROOT/.venv" ]; then
  echo "  Creating .venv …"
  python3 -m venv "$ROOT/.venv"
fi

if [ -x "$ROOT/.venv/bin/python3" ]; then
  PY="$ROOT/.venv/bin/python3"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  echo "  ✗ .venv exists but no python binary found under .venv/bin/"
  exit 1
fi

echo "  Installing / updating dependencies into .venv …"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt"

if ! "$PY" -c "import pyautogui" 2>/dev/null; then
  echo "  ✗ pyautogui still not importable after pip install. See errors above."
  exit 1
fi

echo "  ✓ venv ready: $PY"
echo ""

export APP_MODE=development
exec "$PY" "$ROOT/dashboard.py"
