#!/usr/bin/env bash
# Build the internal developer Trainer app.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --upgrade pip
python3 -m pip install "pyinstaller>=6.0"
pyinstaller packaging/trainer_app.spec
echo "Output: dist/CusearTrainerApp/ — internal studio only; do not ship to customers."
