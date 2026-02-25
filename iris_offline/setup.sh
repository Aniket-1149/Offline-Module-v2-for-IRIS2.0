#!/usr/bin/env bash
# =============================================================================
# setup.sh — IRIS 2.0 Offline Module — Raspberry Pi 5 Setup Script
# Run as root: sudo bash setup.sh
# =============================================================================
set -euo pipefail

INSTALL_DIR="/opt/iris_offline"
VENV_DIR="${INSTALL_DIR}/venv"
SERVICE_FILE="iris_offline.service"
IRIS_USER="iris"
LOG_FILE="/var/log/iris_offline.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERR]${NC}   $*"; exit 1; }

# ─── Root check ──────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run this script as root: sudo bash setup.sh"

info "=== IRIS 2.0 Offline Module — Setup Starting ==="

# ─── 1. System update ─────────────────────────────────────────────────────
info "Step 1/12: Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# ─── 2. System dependencies ────────────────────────────────────────────────
info "Step 2/12: Installing system dependencies..."
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    python3-opencv \
    libopencv-dev \
    libatlas-base-dev libhdf5-dev \
    libopenblas-dev libblas-dev liblapack-dev \
    libjpeg-dev libpng-dev \
    i2c-tools \
    libcamera-dev libcamera-apps \
    v4l-utils \
    openssl \
    git \
    htop iotop \
    logrotate

# ─── 3. Enable I2C, Camera, GPU memory ────────────────────────────────────
info "Step 3/12: Configuring Raspberry Pi interfaces..."

# Enable I2C
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm=on" >> /boot/firmware/config.txt
    info "  I2C enabled in config.txt"
fi

# Enable I2C kernel modules
modprobe i2c-dev i2c-bcm2835 2>/dev/null || true
if ! grep -q "i2c-dev" /etc/modules; then
    echo -e "i2c-dev\ni2c-bcm2835" >> /etc/modules
fi

# Camera: enable on RPi 5 (libcamera stack)
if ! grep -q "camera_auto_detect" /boot/firmware/config.txt 2>/dev/null; then
    echo "camera_auto_detect=1" >> /boot/firmware/config.txt
    info "  Camera auto-detect enabled"
fi

# GPU memory split (128MB minimum for camera)
if ! grep -q "^gpu_mem=128" /boot/firmware/config.txt 2>/dev/null; then
    echo "gpu_mem=128" >> /boot/firmware/config.txt
fi

# ─── 4. Create iris user ──────────────────────────────────────────────────
info "Step 4/12: Creating 'iris' system user..."
if ! id -u "${IRIS_USER}" &>/dev/null; then
    useradd -r -s /bin/false -G gpio,i2c,video,dialout "${IRIS_USER}"
    info "  User '${IRIS_USER}' created"
else
    warn "  User '${IRIS_USER}' already exists"
fi
usermod -aG gpio,i2c,video,dialout "${IRIS_USER}" 2>/dev/null || true

# ─── 5. Install directory ─────────────────────────────────────────────────
info "Step 5/12: Creating install directory ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp -r ./* "${INSTALL_DIR}/" 2>/dev/null || true
chown -R "${IRIS_USER}:${IRIS_USER}" "${INSTALL_DIR}"

# ─── 6. Python virtual environment ────────────────────────────────────────
info "Step 6/12: Creating Python virtual environment..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools -q

# ─── 7. Python dependencies ───────────────────────────────────────────────
info "Step 7/12: Installing Python dependencies (this may take several minutes)..."

# Numpy: use system package first for speed, then pip for exact version
apt-get install -y -qq python3-numpy || true

"${VENV_DIR}/bin/pip" install --no-cache-dir \
    "numpy>=1.24,<2.0" \
    "opencv-python-headless>=4.8" \
    "ultralytics>=8.0.200" \
    "flask>=3.0" \
    "pyopenssl>=23.0" \
    "cryptography>=41.0" \
    "smbus2>=0.4" \
    "RPi.GPIO>=0.7" \
    "werkzeug>=3.0" \
    -q

info "  Downloading YOLOv8n model..."
"${VENV_DIR}/bin/python" -c "
from ultralytics import YOLO
YOLO('yolov8n.pt')
print('YOLOv8n downloaded and cached')
" && mv ~/.cache/ultralytics/*/yolov8n.pt "${INSTALL_DIR}/yolov8n.pt" 2>/dev/null || \
"${VENV_DIR}/bin/python" -c "
from ultralytics import YOLO
import shutil, pathlib
m = YOLO('yolov8n.pt')
src = pathlib.Path('yolov8n.pt')
if src.exists():
    shutil.copy(src, '${INSTALL_DIR}/yolov8n.pt')
"

# ─── 8. Generate TLS certificate ─────────────────────────────────────────
info "Step 8/12: Generating self-signed TLS certificate (3 years)..."
PI_IP=$(hostname -I | awk '{print $1}')
openssl req -x509 -newkey rsa:4096 -sha256 -days 1095 \
    -nodes \
    -keyout "${INSTALL_DIR}/key.pem" \
    -out    "${INSTALL_DIR}/cert.pem" \
    -subj   "/CN=iris-offline/O=IRIS2/C=US" \
    -addext "subjectAltName=IP:${PI_IP},IP:127.0.0.1,DNS:localhost"

chmod 640 "${INSTALL_DIR}/key.pem" "${INSTALL_DIR}/cert.pem"
chown "${IRIS_USER}:${IRIS_USER}" "${INSTALL_DIR}/key.pem" "${INSTALL_DIR}/cert.pem"
info "  TLS cert generated for IP: ${PI_IP}"

# ─── 9. Log file ──────────────────────────────────────────────────────────
info "Step 9/12: Setting up log file..."
touch "${LOG_FILE}"
chown "${IRIS_USER}:${IRIS_USER}" "${LOG_FILE}"
chmod 640 "${LOG_FILE}"

# Logrotate config
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

# ─── 10. Install systemd service ─────────────────────────────────────────
info "Step 10/12: Installing systemd service..."
cp "${INSTALL_DIR}/${SERVICE_FILE}" /etc/systemd/system/
systemctl daemon-reload
systemctl enable iris_offline.service
info "  Service enabled (will auto-start on boot)"

# ─── 11. i2cdetect verification ──────────────────────────────────────────
info "Step 11/12: Scanning I2C bus..."
if command -v i2cdetect &>/dev/null; then
    echo "--- i2cdetect -y 1 output ---"
    i2cdetect -y 1 2>/dev/null || warn "i2cdetect failed — I2C may not be active until reboot"
fi

# ─── 12. Performance tuning ──────────────────────────────────────────────
info "Step 12/12: Applying performance optimisations..."

# Disable swap (prevent swap thrash with model in RAM)
# Only disable if we have enough RAM — Pi 5 8GB is sufficient
swapoff -a 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true

# Set CPU governor to performance
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
    echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null
    info "  CPU governor: performance"
fi

# Increase I2C bus speed to 400kHz (Fast mode) for MPU9250
if ! grep -q "^dtparam=i2c_arm_baudrate=400000" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm_baudrate=400000" >> /boot/firmware/config.txt
    info "  I2C speed set to 400kHz"
fi

echo ""
info "=== Setup Complete ==="
echo ""
echo "  Install directory : ${INSTALL_DIR}"
echo "  Python venv       : ${VENV_DIR}"
echo "  TLS certificate   : ${INSTALL_DIR}/cert.pem"
echo "  JSON endpoint     : https://${PI_IP}:5000/vision"
echo "  Stream endpoint   : https://${PI_IP}:5000/stream"
echo "  Log file          : ${LOG_FILE}"
echo ""
warn "A REBOOT IS REQUIRED to activate I2C and camera changes."
echo ""
echo "  After reboot, start manually:"
echo "    sudo systemctl start iris_offline"
echo "  Or reboot for auto-start:"
echo "    sudo reboot"
echo ""
echo "  Check status:   sudo systemctl status iris_offline"
echo "  Watch logs:     sudo journalctl -u iris_offline -f"
