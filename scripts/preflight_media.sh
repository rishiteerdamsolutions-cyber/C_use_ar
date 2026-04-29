#!/usr/bin/env bash
set -euo pipefail

WORKFLOW="${1:-}"
TOPIC_SEED="${2:-}"
INDUSTRY="${3:-}"
API_BASE="${API_BASE:-http://127.0.0.1:7788}"
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-10}"
CURL_MAX_TIME="${CURL_MAX_TIME:-180}"

if [[ -z "${WORKFLOW}" ]]; then
  echo "Usage: ./scripts/preflight_media.sh <workflow_name> [topic_seed] [industry]"
  exit 1
fi

echo "== cusear media preflight =="
echo "Workflow: ${WORKFLOW}"
echo "API: ${API_BASE}"

payload=$(WF="${WORKFLOW}" TOPIC="${TOPIC_SEED}" IND="${INDUSTRY}" python3 - <<'PY'
import json, os
print(json.dumps({
  "workflow_name": os.environ["WF"],
  "topic_seed": os.environ.get("TOPIC", ""),
  "industry": os.environ.get("IND", ""),
}))
PY
)

echo "-- Run preflight"
if ! curl -sS --connect-timeout "${CURL_CONNECT_TIMEOUT}" --max-time "${CURL_MAX_TIME}" \
  -X POST "${API_BASE}/campaign/preflight" \
  -H "Content-Type: application/json" \
  --data "${payload}" > /tmp/cusear_preflight_run.json; then
  echo "Preflight request failed or timed out after ${CURL_MAX_TIME}s."
  exit 2
fi
cat /tmp/cusear_preflight_run.json
echo

echo "-- Fetch report"
if ! curl -sS --connect-timeout "${CURL_CONNECT_TIMEOUT}" --max-time "${CURL_MAX_TIME}" \
  "${API_BASE}/media/preflight_report?workflow=${WORKFLOW}" > /tmp/cusear_preflight_report.json; then
  echo "Report request failed or timed out after ${CURL_MAX_TIME}s."
  exit 3
fi
cat /tmp/cusear_preflight_report.json
echo

echo "-- Summary (days 1-7 image + 1-30 text)"
python3 - <<'PY'
import json
from pathlib import Path
r = json.loads(Path("/tmp/cusear_preflight_report.json").read_text())
days = ((r.get("report") or {}).get("days") or [])
def ok(d):
    di = int(d.get("day_index") or 0)
    t = bool(d.get("topic_ok"))
    c = bool(d.get("caption_ok"))
    i = bool(d.get("image_ok"))
    text_ok = t and c if 1 <= di <= 30 else True
    image_ok = i if 1 <= di <= 7 else True
    return text_ok and image_ok
bad = [d for d in days if not ok(d)]
print(f"Total rows: {len(days)}")
print(f"Fail rows: {len(bad)}")
for d in bad[:20]:
    print(f"day {d.get('day_index')}: errors={d.get('errors')}")
PY

echo "Done."
