#!/bin/bash
# Cusear™ local agent launcher (macOS) — no code signing required.
# Double-click after download, or run: bash mac.command
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
TOKEN_FILE="$DIR/agent-token.txt"
WS_BASE="${CUSEAR_WS_BASE:-wss://api.cusear.autos}"
REPO_HINT="${CUSEAR_REPO_ROOT:-$HOME/cusear-agent}"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo ""
  echo "  Paste your agent token from the Cusear app (Install step), save as:"
  echo "  $TOKEN_FILE"
  echo ""
  read -r -p "Or paste token now: " TOKEN
  if [[ -z "${TOKEN:-}" ]]; then
    echo "No token. Open https://app.cusear.autos/app/ and copy from Install."
    read -r -p "Press Enter to close…"
    exit 1
  fi
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
fi

TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
export CUSEAR_WS_BASE="$WS_BASE"

AGENT_PY=""
for candidate in "$REPO_HINT/cusear_agent.py" "$DIR/../cusear_agent.py" "$DIR/cusear_agent.py"; do
  if [[ -f "$candidate" ]]; then
    AGENT_PY="$candidate"
    break
  fi
done

if [[ -z "$AGENT_PY" ]]; then
  echo ""
  echo "  cusear_agent.py not found."
  echo "  1) Clone the repo to $REPO_HINT"
  echo "  2) pip install -r requirements.txt"
  echo "  3) Re-run this launcher"
  echo ""
  read -r -p "Press Enter to close…"
  exit 1
fi

cd "$(dirname "$AGENT_PY")"
echo "Starting Cusear agent (keep this window open)…"
exec python3 "$AGENT_PY" "$TOKEN"
