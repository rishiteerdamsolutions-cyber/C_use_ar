#!/usr/bin/env bash
# Build a distributable desktop folder (PyInstaller one-folder bundle).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --upgrade pip
python3 -m pip install "pyinstaller>=6.0"
pyinstaller packaging/desktop_app.spec
echo "Output: dist/AutonomousWebAgencyDesktop/ — run the executable inside; keep workflows/ and .env.local beside it when distributing."
echo "Optional: ENTITLEMENTS_ENFORCEMENT, ENTITLED_MODULES, TRAINER_AI_RUNS_MONTHLY_CAP, DESKTOP_LICENSE_CHECK — see .env.local.example"
