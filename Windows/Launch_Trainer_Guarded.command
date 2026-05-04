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

echo "Starting cusear™ Trainer (GUARDED RUN mode) using: $PY_BIN"
echo ""
echo "  Guarded mode adds a pause before every step and validates click targets before clicking."
echo "  If the platform UX drifted (banners/modals/redesign), the run aborts BEFORE the click."
echo ""
echo "  Tuning:"
echo "    export TRAINER_GUARD_STEP_DELAY_SECONDS=10        # 0–60 (default 10)"
echo "    export TRAINER_GUARD_MAX_DISTANCE_PX=160          # compare vs saved coords (default 160)"
echo ""

export AGENCY_USER_MODE=trainer
export TRAINER_GUARDED_RUN=1
"$PY_BIN" desktop.py

echo
echo "Trainer exited."
echo "Press Enter to close."
read -r

