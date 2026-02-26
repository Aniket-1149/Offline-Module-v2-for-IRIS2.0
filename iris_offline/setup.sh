#!/usr/bin/env bash
# =============================================================================
# setup.sh — IRIS 2.0 Offline Module — Raspberry Pi 5 Setup / Re-setup
#
# First-time install:
#   sudo bash setup.sh https://github.com/thekaushal01/Offline-Module-v2-for-IRIS2.0.git
#
# Re-run after code changes (or after git push):
#   sudo bash /opt/iris_offline/setup.sh
#
# This script is fully idempotent — safe to run multiple times.
# On re-runs it detects the existing install, pulls new code, and restarts.
# =============================================================================
set -euo pipefail

# ─── Config ───────────────────────────────────────────────────────────────────
REPO_URL="${1:-}"                     # git repo URL — required first time, saved after that
REPO_DIR="/opt/iris_offline-repo"     # git working directory (full repo clone lives here)
INSTALL_DIR="/opt/iris_offline"       # runtime directory (app files only, no .git overhead)
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_FILE="iris_offline.service"
IRIS_USER="iris"
LOG_FILE="/var/log/iris_offline.log"
CONFIG_FILE="${INSTALL_DIR}/.iris_config"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; exit 1; }
section() { echo -e "${CYAN}────────────────────────────────────────────────────────${NC}"; }

# ─── Root check ───────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run this script as root:  sudo bash setup.sh [repo_url]"

# ─── Restore saved REPO_URL from previous run if not provided ────────────────
if [[ -z "$REPO_URL" && -f "$CONFIG_FILE" ]]; then
    REPO_URL=$(grep '^REPO_URL=' "${CONFIG_FILE}" | cut -d= -f2- || true)
fi
if [[ -z "$REPO_URL" && -d "${REPO_DIR}/.git" ]]; then
    REPO_URL=$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)
fi

# ─── Detect fresh install vs. update ─────────────────────────────────────────
FRESH_INSTALL=true
if [[ -d "${INSTALL_DIR}" && -f "${INSTALL_DIR}/main.py" ]]; then
    FRESH_INSTALL=false
fi

# ─── Banner ───────────────────────────────────────────────────────────────────
section
if $FRESH_INSTALL; then
    info "=== IRIS 2.0 — Full Setup Starting ==="
else
    info "=== IRIS 2.0 — Update / Re-setup Starting ==="
    info "    Existing install at ${INSTALL_DIR} detected."
    info "    Hardware config and certificates will be preserved."
fi
section

# =============================================================================
# FULL INSTALL STEPS — only run on first install
# =============================================================================
if $FRESH_INSTALL; then

    # ─── 1. System update ─────────────────────────────────────────────────────
    info "Step 1/12: Updating system packages..."
    apt-get update -qq
    apt-get upgrade -y -qq

    # ─── 2. System dependencies ───────────────────────────────────────────────
    info "Step 2/12: Installing system dependencies..."
    apt-get install -y -qq \
        python3 python3-pip python3-venv python3-dev \
        python3-opencv python3-picamera2 \
        libopencv-dev \
        python3-lgpio lgpio \
        libhdf5-dev \
        libopenblas-dev libblas-dev liblapack-dev \
        libjpeg-dev libpng-dev \
        i2c-tools libcamera-dev rpicam-apps \
        openssl git rsync htop iotop logrotate

    # ─── 3. Enable I2C, Camera, GPU memory ───────────────────────────────────
    info "Step 3/12: Configuring Raspberry Pi interfaces..."

    if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
        echo "dtparam=i2c_arm=on" >> /boot/firmware/config.txt
        info "  I2C enabled"
    fi
    modprobe i2c-dev i2c-bcm2835 2>/dev/null || true
    if ! grep -q "i2c-dev" /etc/modules; then
        echo -e "i2c-dev\ni2c-bcm2835" >> /etc/modules
    fi
    if ! grep -q "camera_auto_detect" /boot/firmware/config.txt 2>/dev/null; then
        echo "camera_auto_detect=1" >> /boot/firmware/config.txt
        info "  Camera auto-detect enabled"
    fi
    if ! grep -q "^gpu_mem=128" /boot/firmware/config.txt 2>/dev/null; then
        echo "gpu_mem=128" >> /boot/firmware/config.txt
    fi
    if ! grep -q "^dtparam=i2c_arm_baudrate=400000" /boot/firmware/config.txt 2>/dev/null; then
        echo "dtparam=i2c_arm_baudrate=400000" >> /boot/firmware/config.txt
        info "  I2C speed set to 400kHz"
    fi

    # ─── 4. Create iris system user ──────────────────────────────────────────
    info "Step 4/12: Creating 'iris' system user..."
    if ! id -u "${IRIS_USER}" &>/dev/null; then
        useradd -r -s /bin/false -G gpio,i2c,video,dialout "${IRIS_USER}"
        info "  User '${IRIS_USER}' created"
    else
        warn "  User '${IRIS_USER}' already exists — skipping"
    fi
    usermod -aG gpio,i2c,video,dialout "${IRIS_USER}" 2>/dev/null || true

fi  # end FRESH_INSTALL-only steps

# =============================================================================
# STEPS THAT RUN ON EVERY SETUP AND UPDATE
# =============================================================================

# ─── 5. Sync code ─────────────────────────────────────────────────────────────
info "Step 5: Syncing latest code to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

if [[ -n "${REPO_URL}" ]]; then
    if [[ -d "${REPO_DIR}/.git" ]]; then
        info "  Pulling latest changes from git..."
        git -C "${REPO_DIR}" pull --ff-only
    else
        info "  Cloning repository..."
        git clone "${REPO_URL}" "${REPO_DIR}"
    fi
    # rsync only app files — certs and model are excluded so they survive updates
    rsync -a --delete \
        --exclude='venv/' \
        --exclude='*.pem' \
        --exclude='yolov8n.pt' \
        --exclude='yolov8n.onnx' \
        --exclude='.iris_config' \
        "${REPO_DIR}/iris_offline/" "${INSTALL_DIR}/"
    # Save repo URL for future re-runs
    echo "REPO_URL=${REPO_URL}" > "${CONFIG_FILE}"
    info "  Code synced from ${REPO_URL}"
else
    # Fallback: copy from the directory this script is running from
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    rsync -a --exclude='venv/' --exclude='*.pem' --exclude='yolov8n.pt' --exclude='yolov8n.onnx' \
        "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
    warn "  No REPO_URL provided — copied from ${SCRIPT_DIR}"
    warn "  To enable one-command updates next time, re-run with your repo URL:"
    warn "    sudo bash setup.sh https://github.com/thekaushal01/Offline-Module-v2-for-IRIS2.0.git"
fi

chown -R "${IRIS_USER}:${IRIS_USER}" "${INSTALL_DIR}"
# Allow the logged-in user (pi) to run the code directly without sudo -u iris
chmod -R o+rX "${INSTALL_DIR}"

# ─── 6. Python virtual environment ────────────────────────────────────────────
info "Step 6: Setting up Python virtual environment..."
# Must use --system-site-packages so the venv can see python3-picamera2 (system package)
if [[ -d "${VENV_DIR}" ]]; then
    if ! grep -q "include-system-site-packages = true" "${VENV_DIR}/pyvenv.cfg" 2>/dev/null; then
        warn "  Recreating venv with --system-site-packages (required for picamera2)..."
        rm -rf "${VENV_DIR}"
    fi
fi
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv --system-site-packages "${VENV_DIR}"
    info "  Virtual environment created"
else
    info "  Virtual environment OK (system-site-packages enabled)"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools -q

# ─── 7. Python dependencies ───────────────────────────────────────────────────
info "Step 7: Installing/updating Python dependencies..."
apt-get install -y -qq python3-numpy 2>/dev/null || true

"${VENV_DIR}/bin/pip" install --no-cache-dir \
    "numpy>=1.24,<2.0" \
    "opencv-python-headless>=4.8" \
    "ultralytics>=8.0.200" \
    "flask>=3.0" \
    "pyopenssl>=23.0" \
    "cryptography>=41.0" \
    "smbus2>=0.4" \
    "rpi-lgpio" \
    "werkzeug>=3.0" \
    -q

# ultralytics pulls in opencv-python (Qt GUI build) as a dependency —
# force it back to headless so we don't need a display to import cv2
"${VENV_DIR}/bin/pip" install --force-reinstall "opencv-python-headless>=4.8" -q
info "  Forced opencv-python-headless (removes Qt dependency from ultralytics)"

# Download YOLOv8n model only if not already present
if [[ ! -f "${INSTALL_DIR}/yolov8n.pt" ]]; then
    info "  Downloading YOLOv8n model (~6MB)..."
    "${VENV_DIR}/bin/python" -c "
from ultralytics import YOLO
import shutil, pathlib
YOLO('yolov8n.pt')
src = pathlib.Path('yolov8n.pt')
if src.exists():
    shutil.copy(src, '${INSTALL_DIR}/yolov8n.pt')
"
else
    info "  YOLOv8n model already present — skipping download"
fi

# ─── 8. TLS certificate ───────────────────────────────────────────────────────
# Skip if cert exists and won't expire within 30 days
if [[ ! -f "${INSTALL_DIR}/cert.pem" ]] || \
   ! openssl x509 -checkend $((30*86400)) -noout -in "${INSTALL_DIR}/cert.pem" &>/dev/null; then
    info "Step 8: Generating TLS certificate (3 years)..."
    PI_IP=$(hostname -I | awk '{print $1}')
    openssl req -x509 -newkey rsa:4096 -sha256 -days 1095 \
        -nodes \
        -keyout "${INSTALL_DIR}/key.pem" \
        -out    "${INSTALL_DIR}/cert.pem" \
        -subj   "/CN=iris-offline/O=IRIS2/C=US" \
        -addext "subjectAltName=IP:${PI_IP},IP:127.0.0.1,DNS:localhost"
    chmod 640 "${INSTALL_DIR}/key.pem" "${INSTALL_DIR}/cert.pem"
    chown "${IRIS_USER}:${IRIS_USER}" "${INSTALL_DIR}/key.pem" "${INSTALL_DIR}/cert.pem"
    info "  Certificate generated for IP: ${PI_IP}"
else
    info "Step 8: TLS certificate is valid — skipping regeneration"
fi

# ─── 9. Log file and logrotate ────────────────────────────────────────────────
if [[ ! -f "${LOG_FILE}" ]]; then
    info "Step 9: Setting up log file..."
    touch "${LOG_FILE}"
    chown "${IRIS_USER}:${IRIS_USER}" "${LOG_FILE}"
    chmod 640 "${LOG_FILE}"
    cat > /etc/logrotate.d/iris_offline << 'EOF'
/var/log/iris_offline.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 640 iris iris
}
EOF
fi

# ─── 10. Install systemd service file (not enabled — manual run preferred) ───
info "Step 10: Installing systemd service file..."
cp "${INSTALL_DIR}/${SERVICE_FILE}" /etc/systemd/system/
systemctl daemon-reload
info "  Service file installed (not enabled — run IRIS manually, see below)"

# =============================================================================
# FRESH INSTALL ONLY — final checks and tuning
# =============================================================================
if $FRESH_INSTALL; then

    # ─── 11. I2C detection ────────────────────────────────────────────────────
    info "Step 11/12: Scanning I2C bus..."
    if command -v i2cdetect &>/dev/null; then
        echo "--- i2cdetect -y 1 ---"
        i2cdetect -y 1 2>/dev/null || warn "  i2cdetect failed — I2C will be active after reboot"
    fi

    # ─── 12. Performance tuning ───────────────────────────────────────────────
    info "Step 12/12: Applying performance optimisations..."
    swapoff -a 2>/dev/null || true
    systemctl disable dphys-swapfile 2>/dev/null || true
    if [[ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]]; then
        echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null
        info "  CPU governor set to performance"
    fi

fi

# ─── Summary ──────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')
section
if $FRESH_INSTALL; then
    info "=== Setup Complete ==="
else
    info "=== Update Complete ==="
fi
echo ""
echo "  Install directory : ${INSTALL_DIR}"
echo "  Python venv       : ${VENV_DIR}"
echo "  TLS certificate   : ${INSTALL_DIR}/cert.pem"
echo "  JSON endpoint     : https://${PI_IP}:5000/vision"
echo "  Stream endpoint   : https://${PI_IP}:5000/stream"
echo "  Log file          : ${LOG_FILE}"
if [[ -n "${REPO_URL}" ]]; then
    echo "  Git remote        : ${REPO_URL}"
fi
echo ""

if $FRESH_INSTALL; then
    warn "REBOOT REQUIRED to activate I2C and camera interface changes."
    echo ""
    echo "  After reboot, run IRIS manually:"
    echo "    /opt/iris_offline/venv/bin/python /opt/iris_offline/main.py"
    echo ""
    if [[ -n "${REPO_URL}" ]]; then
        echo "  ─── Updating later ──────────────────────────────────────────"
        echo "  After you commit and push code changes, re-run on the Pi:"
        echo "    sudo bash ${INSTALL_DIR}/update.sh"
    fi
else
    echo "  Code is up to date. Run IRIS manually:"
    echo "    /opt/iris_offline/venv/bin/python /opt/iris_offline/main.py"
fi
section
