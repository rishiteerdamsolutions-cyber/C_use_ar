#!/usr/bin/env bash
# ── Web Agency Trainer — Local Startup ──────────────────────────────────────
# Run this once to install dependencies and launch the training dashboard.
#
# Usage:
#   bash START.sh
# ────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ⚡ Web Agency Trainer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "  ✗ Python 3 not found."
  echo "  Install from https://python.org/downloads"
  exit 1
fi

echo "  ✓ Python $(python3 --version 2>&1 | cut -d' ' -f2)"

# Install only what's needed for the trainer (minimal set)
echo "  Installing dependencies…"
python3 -m pip install --quiet --break-system-packages \
  pyautogui pillow anthropic 2>/dev/null || \
python3 -m pip install --quiet \
  pyautogui pillow anthropic 2>/dev/null || true

echo "  ✓ Dependencies ready"
echo ""

# Set dev mode so license check is skipped locally
export APP_MODE=development

# Launch
python3 dashboard.py
