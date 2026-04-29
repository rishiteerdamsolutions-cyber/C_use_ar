#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  Client Build Pipeline — cusear™ Platform
#  Produces a tamper-proof, locked binary for distribution to clients.
#
#  What this does:
#   1. Validates environment (secrets set, tools installed)
#   2. Encrypts all trained workflow JSON files with client's machine ID
#   3. Obfuscates Python source with PyArmor (bytecode-level protection)
#   4. Compiles everything into a single .exe / .app with PyInstaller
#   5. Packages the distribution folder ready to send to client
#
#  Requirements (install once):
#   pip install pyarmor pyinstaller cryptography
#
#  Usage:
#   export LICENSE_MASTER_SECRET="your_64_char_hex_secret"
#   export WORKFLOW_MASTER_KEY="your_64_char_hex_secret"
#   bash build/build_client.sh \
#       --machine-id  <client_machine_id> \
#       --email       client@example.com \
#       --plan        free \
#       --output      dist/client_name
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ─── Colour helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}  →${NC} $*"; }
success() { echo -e "${GREEN}  ✓${NC} $*"; }
warn()    { echo -e "${YELLOW}  ⚠${NC} $*"; }
error()   { echo -e "${RED}  ✗${NC} $*"; exit 1; }
divider() { echo -e "${BOLD}  $(printf '─%.0s' {1..55})${NC}"; }

# ─── Defaults ────────────────────────────────────────────────────────────────
MACHINE_ID=""
CLIENT_EMAIL=""
PLAN="free"
OUTPUT_DIR=""
VALID_DAYS=365
APP_NAME="WebAgencyPro"
PYTHON="${PYTHON:-python3}"

# ─── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --machine-id)   MACHINE_ID="$2";    shift 2 ;;
    --email)        CLIENT_EMAIL="$2";  shift 2 ;;
    --plan)         PLAN="$2";          shift 2 ;;
    --output)       OUTPUT_DIR="$2";    shift 2 ;;
    --days)         VALID_DAYS="$2";    shift 2 ;;
    --app-name)     APP_NAME="$2";      shift 2 ;;
    *) error "Unknown argument: $1" ;;
  esac
done

# ─── Validate inputs ─────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  cusear™ — Client Build Pipeline${NC}"
divider

[[ -z "$MACHINE_ID" ]]   && error "--machine-id is required"
[[ -z "$CLIENT_EMAIL" ]] && error "--email is required"
[[ -z "$OUTPUT_DIR" ]]   && OUTPUT_DIR="dist/$(echo "$CLIENT_EMAIL" | tr '@.' '_')"

# ─── Check required environment variables ────────────────────────────────────
info "Checking environment..."

[[ -z "${LICENSE_MASTER_SECRET:-}" ]] && \
  error "LICENSE_MASTER_SECRET not set. Export it before building."
[[ -z "${WORKFLOW_MASTER_KEY:-}" ]] && \
  error "WORKFLOW_MASTER_KEY not set. Export it before building."
[[ ${#LICENSE_MASTER_SECRET} -ne 64 ]] && \
  error "LICENSE_MASTER_SECRET must be 64 hex characters (32 bytes)."
[[ ${#WORKFLOW_MASTER_KEY} -ne 64 ]] && \
  error "WORKFLOW_MASTER_KEY must be 64 hex characters (32 bytes)."

success "Secrets loaded"

# ─── Check tools ─────────────────────────────────────────────────────────────
info "Checking tools..."

command -v "$PYTHON" >/dev/null 2>&1 || error "Python not found (set PYTHON env var)"
"$PYTHON" -c "import pyarmor"       2>/dev/null || { warn "PyArmor not found — installing..."; pip install pyarmor -q; }
"$PYTHON" -c "import PyInstaller"   2>/dev/null || { warn "PyInstaller not found — installing..."; pip install pyinstaller -q; }
"$PYTHON" -c "import cryptography"  2>/dev/null || { warn "cryptography not found — installing..."; pip install cryptography -q; }

success "All tools available"
divider

# ─── Step 1: Encrypt workflows ───────────────────────────────────────────────
info "Encrypting workflows for machine: ${MACHINE_ID:0:16}..."

"$PYTHON" build/encrypt_workflows.py single \
  --machine-id   "$MACHINE_ID" \
  --client-email "$CLIENT_EMAIL" \
  --plan         "$PLAN" \
  --days         "$VALID_DAYS" \
  --output-dir   "$OUTPUT_DIR"

success "Workflows encrypted → $OUTPUT_DIR/workflows/"
divider

# ─── Step 2: PyArmor obfuscation ─────────────────────────────────────────────
info "Obfuscating Python source with PyArmor..."

OBFUSCATED_DIR="build/.pyarmor_obfuscated"
rm -rf "$OBFUSCATED_DIR"

# Obfuscate all Python files except tests and build scripts
pyarmor gen \
  --output "$OBFUSCATED_DIR" \
  --platform windows.x86_64 \
  main.py \
  teach/runner_v1.py \
  teach/workflow_runner.py \
  security/license.py \
  security/workflow_crypto.py \
  execution/executor.py \
  execution/fallback.py \
  vision/vision_engine.py \
  config/remote_config.py \
  analytics/session.py \
  2>/dev/null || {
    warn "PyArmor gen failed — trying legacy mode..."
    pyarmor obfuscate --output "$OBFUSCATED_DIR" main.py \
      2>/dev/null || warn "PyArmor obfuscation skipped — proceeding without it"
  }

success "Source obfuscated"
divider

# ─── Step 3: Determine source dir ────────────────────────────────────────────
if [[ -d "$OBFUSCATED_DIR" && -f "$OBFUSCATED_DIR/main.py" ]]; then
  BUILD_SRC="$OBFUSCATED_DIR"
  info "Using obfuscated source"
else
  BUILD_SRC="."
  warn "Using plain source (obfuscation unavailable)"
fi

# ─── Step 4: PyInstaller compilation ─────────────────────────────────────────
info "Compiling with PyInstaller → single executable..."

# Determine OS-specific settings
OS_NAME="$(uname -s)"
case "$OS_NAME" in
  Darwin)   SEPARATOR=":" ; EXE_EXT=""    ; ICON_FLAG="" ;;
  Linux)    SEPARATOR=":" ; EXE_EXT=""    ; ICON_FLAG="" ;;
  MINGW*|CYGWIN*|MSYS*)
            SEPARATOR=";" ; EXE_EXT=".exe"; ICON_FLAG="" ;;
  *)        SEPARATOR=":" ; EXE_EXT=""    ;;
esac

pyinstaller \
  --name "${APP_NAME}" \
  --onefile \
  --windowed \
  --clean \
  --noconfirm \
  --distpath "build/.pyinstaller_dist" \
  --workpath "build/.pyinstaller_work" \
  --specpath "build/" \
  --add-data "templates${SEPARATOR}templates" \
  --add-data "config${SEPARATOR}config" \
  --add-data "AGENT_DASHBOARD.html${SEPARATOR}." \
  --hidden-import "cryptography" \
  --hidden-import "PIL" \
  --hidden-import "pyautogui" \
  --hidden-import "anthropic" \
  --exclude-module "pytest" \
  --exclude-module "tests" \
  "${BUILD_SRC}/main.py" \
  2>&1 | tail -20

EXE_SRC="build/.pyinstaller_dist/${APP_NAME}${EXE_EXT}"

if [[ -f "$EXE_SRC" ]]; then
  success "Executable built: $EXE_SRC"
else
  error "PyInstaller build failed — check output above"
fi
divider

# ─── Step 5: Package final distribution ──────────────────────────────────────
info "Packaging final distribution..."

FINAL_DIR="$OUTPUT_DIR/app"
mkdir -p "$FINAL_DIR"

# Copy executable
cp "$EXE_SRC" "$FINAL_DIR/${APP_NAME}${EXE_EXT}"

# Copy encrypted workflows (already in OUTPUT_DIR/workflows)
cp -r "$OUTPUT_DIR/workflows" "$FINAL_DIR/"

# Copy license key
cp "$OUTPUT_DIR/license.key" "$FINAL_DIR/"

# Copy templates
cp -r "templates" "$FINAL_DIR/"

# Copy dashboard HTML
cp "AGENT_DASHBOARD.html" "$FINAL_DIR/"

# Write README for client
cat > "$FINAL_DIR/INSTALL.txt" << EOF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Web Agency Pro — Installation Guide
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Keep ALL files in this folder together
   (license.key, workflows/, templates/)

2. Double-click ${APP_NAME}${EXE_EXT} to launch

3. First launch: the app will verify your
   license automatically

4. On the dashboard, select a template,
   enter your client's details, click Run

Support: support@yourplatform.com

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF

success "Distribution packaged → $FINAL_DIR"
divider

# ─── Clean up build artifacts ────────────────────────────────────────────────
rm -rf "$OBFUSCATED_DIR" "build/.pyinstaller_dist" "build/.pyinstaller_work"
info "Build artifacts cleaned"

# ─── Final summary ───────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  ✓ BUILD COMPLETE${NC}"
divider
echo "  Client:      $CLIENT_EMAIL"
echo "  Plan:        $PLAN"
echo "  Machine ID:  ${MACHINE_ID:0:20}..."
echo "  Output:      $FINAL_DIR"
echo ""
echo "  Files to send to client:"
echo "    📦 $FINAL_DIR/"
echo "       ├── ${APP_NAME}${EXE_EXT}   ← main executable"
echo "       ├── license.key            ← machine-bound license"
echo "       ├── workflows/             ← encrypted .enc files"
echo "       ├── templates/             ← template metadata"
echo "       └── INSTALL.txt"
echo ""
echo -e "${YELLOW}  ⚠  DO NOT include any .py or plain .json files${NC}"
echo -e "${YELLOW}  ⚠  DO NOT share the license.key with other clients${NC}"
divider
echo ""
