#!/usr/bin/env bash
# Vercel build (keeps vercel.json buildCommand under 256 chars).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
rm -rf public
mkdir -p public
cp -R "CUSEAR WEBSITE  UX UI/cusear-website/." public/
mkdir -p public/downloads
cp -f "downloads/cusear-desktop-windows-setup.exe" "public/downloads/" 2>/dev/null || true
cp -f "downloads/cusear-desktop-macos.dmg" "public/downloads/" 2>/dev/null || true
