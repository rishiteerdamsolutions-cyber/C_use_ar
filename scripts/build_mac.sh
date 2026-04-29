#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m pip install --upgrade pip
python3 -m pip install "pyinstaller>=6.0"

pyinstaller packaging/desktop_app.spec

APP_DIR="dist/AutonomousWebAgencyDesktop"
DMG_NAME="cusear-ar.dmg"
OUT_DMG="dist/${DMG_NAME}"

if command -v create-dmg >/dev/null 2>&1; then
  rm -f "${OUT_DMG}"
  create-dmg "${OUT_DMG}" "${APP_DIR}" >/dev/null
  echo "Built ${OUT_DMG}"
else
  echo "create-dmg not found; build output is ${APP_DIR}"
fi

if [[ -n "${APPLE_DEVELOPER_ID:-}" ]] && command -v codesign >/dev/null 2>&1; then
  echo "Signing app bundle..."
  codesign --force --deep --sign "${APPLE_DEVELOPER_ID}" "${APP_DIR}" || true
fi
