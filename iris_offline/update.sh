#!/usr/bin/env bash
# =============================================================================
# update.sh — IRIS 2.0 Offline Module — Quick Code Update
#
# Run this on the Pi after you commit and push changes from your PC:
#   sudo bash /opt/iris_offline/update.sh
#
# What it does:
#   1. git pull  — fetches your latest committed code
#   2. rsync     — syncs changed files to the runtime directory
#   3. pip sync  — installs any new Python packages (fast if none changed)
#   4. restart   — restarts the systemd service
#   5. status    — shows live service status so you can confirm it's running
#
# Certs (cert.pem/key.pem) and the YOLOv8n model are preserved across updates.
# =============================================================================
set -euo pipefail

INSTALL_DIR="/opt/iris_offline"
REPO_DIR="/opt/iris_offline-repo"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_FILE="${INSTALL_DIR}/.iris_config"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; exit 1; }
section() { echo -e "${CYAN}────────────────────────────────────────────────────────${NC}"; }

# ─── Checks ───────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root:  sudo bash update.sh"
[[ ! -d "${INSTALL_DIR}" ]] && error "IRIS not installed at ${INSTALL_DIR}. Run setup.sh first."
[[ ! -f "${INSTALL_DIR}/main.py" ]] && error "Incomplete install at ${INSTALL_DIR}. Run setup.sh first."

if [[ ! -d "${REPO_DIR}/.git" ]]; then
    error "No git repo at ${REPO_DIR}.
  This means IRIS was installed without a git URL.
  Re-run setup to enable git-based updates:
    sudo bash ${INSTALL_DIR}/setup.sh https://github.com/thekaushal01/Offline-Module-v2-for-IRIS2.0.git"
fi

section
info "=== IRIS 2.0 — Pulling Latest Code ==="
section

# ─── 1. Git pull ──────────────────────────────────────────────────────────────
info "Fetching latest code from git..."
BEFORE=$(git -C "${REPO_DIR}" rev-parse HEAD)
git -C "${REPO_DIR}" pull --ff-only
AFTER=$(git -C "${REPO_DIR}" rev-parse HEAD)

if [[ "${BEFORE}" == "${AFTER}" ]]; then
    warn "Already up to date — no code changes found."
    warn "If you expected changes, check that you pushed from your PC first."
    # Still continue — dependencies or service file may need refreshing
fi

# Show what changed (if anything)
if [[ "${BEFORE}" != "${AFTER}" ]]; then
    echo ""
    echo "  Changed files:"
    git -C "${REPO_DIR}" diff --name-only "${BEFORE}" "${AFTER}" \
        | grep "^iris_offline/" \
        | sed 's|^iris_offline/|    • |'
    echo ""
fi

# ─── 2. Sync files to install directory ───────────────────────────────────────
info "Syncing to ${INSTALL_DIR}..."
rsync -a --delete \
    --exclude='venv/' \
    --exclude='*.pem' \
    --exclude='yolov8n.pt' \
    --exclude='.iris_config' \
    "${REPO_DIR}/iris_offline/" "${INSTALL_DIR}/"
chown -R iris:iris "${INSTALL_DIR}"

# ─── 3. Sync Python packages ──────────────────────────────────────────────────
info "Checking Python dependencies..."
"${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q
info "  Dependencies up to date"

# ─── 4. Reload systemd service file (in case it changed) ──────────────────────
cp "${INSTALL_DIR}/iris_offline.service" /etc/systemd/system/
systemctl daemon-reload

PI_IP=$(hostname -I | awk '{print $1}')
section
info "=== Update Complete ==="
echo ""
echo "  Code is synced. Run IRIS manually:"
echo "    sudo -u iris /opt/iris_offline/venv/bin/python /opt/iris_offline/main.py"
echo ""
echo "  JSON endpoint  : https://${PI_IP}:5000/vision"
section
