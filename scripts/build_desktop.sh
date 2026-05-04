#!/usr/bin/env bash
# Compatibility wrapper: build the internal Trainer desktop folder.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --upgrade pip
python3 -m pip install "pyinstaller>=6.0"
pyinstaller packaging/trainer_app.spec
echo "Output: dist/CusearTrainerApp/ — internal studio only; customer exports use customer_app.py."
