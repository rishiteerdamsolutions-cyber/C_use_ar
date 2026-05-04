#!/usr/bin/env bash
# Create/update a venv with core + Rekky dependencies (Trainer / CLI enrichment).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV="${REKKY_VENV:-$ROOT/.venv-rekky}"

if [[ ! -d "$VENV" ]]; then
  echo "[rekky] Creating venv: $VENV"
  python3 -m venv "$VENV"
fi

PY="$VENV/bin/python"
PIP="$VENV/bin/python -m pip"

echo "[rekky] Upgrading pip..."
"$PY" -m pip install -q --upgrade pip

echo "[rekky] Installing requirements.txt + requirements-rekky.txt..."
"$PY" -m pip install -r "$ROOT/requirements.txt" -r "$ROOT/requirements-rekky.txt"

echo ""
echo "[rekky] Done. Activate with:"
echo "  source $VENV/bin/activate"
echo ""
echo "Quick checks:"
echo "  python -m cusear.engine.rekky --enrich workflows/<YourWorkflow>.json"
echo ""
echo "Permissions:"
echo "  macOS: Accessibility for Terminal/Python + Automation for Google Chrome (AppleScript)."
echo "  Windows: Run Trainer/Python as user with Chrome foreground during enrichment."
