#!/usr/bin/env bash
# Vercel build (keeps vercel.json buildCommand under 256 chars).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
rm -rf public
mkdir -p public

SITE_SRC=""
if [[ -d "cusear-website" ]]; then
  SITE_SRC="cusear-website"
elif [[ -d "CUSEAR WEBSITE  UX UI/cusear-website" ]]; then
  SITE_SRC="CUSEAR WEBSITE  UX UI/cusear-website"
fi
if [[ -n "$SITE_SRC" ]]; then
  cp -R "$SITE_SRC/." public/
fi

if [[ -d portal/pwa ]]; then
  cp -R portal/pwa/. public/app/
  chmod +x public/app/install/mac.command 2>/dev/null || true
elif [[ -f portal/app.html ]]; then
  mkdir -p public/app
  cp -f portal/app.html public/app/index.html
fi

API_BASE="${CUSEAR_API_BASE:-${PUBLIC_API_BASE_URL:-https://api.cusear.autos}}"
SB_URL="${CUSEAR_SUPABASE_URL:-${SUPABASE_URL:-}}"
SB_ANON="${CUSEAR_SUPABASE_ANON_KEY:-}"

cat > public/app/config.js <<EOF
window.CUSEAR_CONFIG = {
  apiBase: "${API_BASE}",
  supabaseUrl: "${SB_URL}",
  supabaseAnonKey: "${SB_ANON}",
  pricingUrl: "/pricing.html",
  siteUrl: "/"
};
window.CUSEAR_API_BASE = window.CUSEAR_CONFIG.apiBase;
window.CUSEAR_SUPABASE_URL = window.CUSEAR_CONFIG.supabaseUrl;
window.CUSEAR_SUPABASE_ANON_KEY = window.CUSEAR_CONFIG.supabaseAnonKey;
EOF

mkdir -p public/downloads
cp -f "downloads/cusear-desktop-windows-setup.exe" "public/downloads/" 2>/dev/null || true
cp -f "downloads/cusear-desktop-macos.dmg" "public/downloads/" 2>/dev/null || true

echo "Built public/ (site=${SITE_SRC:-none}, pwa=portal/pwa)"
