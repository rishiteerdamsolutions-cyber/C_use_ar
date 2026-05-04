#!/usr/bin/env bash
# Build the runner-only customer app shell.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --upgrade pip
python3 -m pip install "pyinstaller>=6.0"
pyinstaller packaging/customer_app.spec
echo "Output: dist/CusearCustomerApp/ — add bundles/, workflows/, .env.local, and license.key before shipping."
