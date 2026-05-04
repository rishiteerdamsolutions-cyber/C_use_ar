#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source ".venv/bin/activate"
python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Tesseract not found. Install it with: brew install tesseract"
  exit 1
fi

echo "Starting Label Pointer (live label → move cursor)."
python label_pointer.py
