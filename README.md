# IRIS 2.0 — Intelligent Real-time Inference System
### An Offline AI Vision Assistant for the Visually Impaired

IRIS 2.0 runs entirely offline on a **Raspberry Pi 5 (8GB)**, helping visually impaired users navigate their environment safely.
It detects nearby objects, measures obstacle distance, monitors for falls, and streams structured data to a companion mobile app — which handles text-to-speech announcements.
No internet connection required. Designed for real-world, long-term wearable or portable use.

---

## What It Does

| Feature | How |
|---|---|
| Object detection | YOLOv8n on CPU at 8–12 FPS |
| Distance measurement | HC-SR04 ultrasonic at 10 Hz |
| Fall detection | MPU9250 IMU via I2C at 50 Hz |
| Live dashboard | OpenCV window on the display |
| JSON API | Flask HTTPS server on port 5000 |

---

## Hardware Required

- Raspberry Pi 5 (8GB)
- Pi Camera Module (any CSI camera — Camera Module v2/v3, HQ Camera, ArduCam)
- HC-SR04 Ultrasonic Sensor
- MPU9250 IMU breakout board
- 1kΩ and 2kΩ resistors (for HC-SR04 ECHO voltage divider)
- Official 27W USB-C PSU

---

## Wiring

### HC-SR04 — Ultrasonic Distance Sensor

| HC-SR04 Pin | Connection | Raspberry Pi Pin | Notes |
|-------------|------------|------------------|---------|
| **VCC** | → | **5V** (Pin 2 or 4) | Power supply |
| **GND** | → | **GND** (Pin 6) | Ground |
| **TRIG** | → | **GPIO23** (Pin 16) | Trigger (3.3V output from Pi is OK) |
| **ECHO** | ⚠️ | **GPIO24** (Pin 18) | **MUST USE VOLTAGE DIVIDER!** |

The HC-SR04 ECHO pin outputs 5V. The Pi GPIO only tolerates 3.3V.
Build this simple voltage divider between ECHO and GPIO24:

```
HC-SR04 ECHO ─── 1kΩ ─── GPIO24 (Pin 18)
                           │
                          2kΩ
                           │
                         GND
```

### MPU9250 — 9-Axis IMU (I2C)

| MPU9250 Pin | Connection | Raspberry Pi Pin | Notes |
|-------------|------------|------------------|---------|
| **VCC** | → | **3.3V** (Pin 1) | ⚠️ 3.3V ONLY! Do not use 5V |
| **GND** | → | **GND** (Pin 6) | Ground |
| **SDA** | → | **GPIO2 (SDA)** (Pin 3) | I2C Data |
| **SCL** | → | **GPIO3 (SCL)** (Pin 5) | I2C Clock |
| **AD0** | ↓ | GND or Float | I2C address (GND = 0x68, VCC = 0x69) |

### Pi Camera (CSI)

Connect via CSI-2 ribbon cable to the **CAM0** port on Pi 5. No GPIO needed.
Works with any Pi CSI camera: Camera Module v2/v3, HQ Camera, ArduCam, etc.

```
iris_offline/
├── main.py               # starts everything, keeps it running
├── vision.py             # camera capture + YOLOv8n inference
├── ultrasonic.py         # HC-SR04 distance polling
├── fall_detection.py     # MPU9250 I2C fall detection
├── ui.py                 # OpenCV live dashboard
├── server.py             # Flask HTTPS JSON server
├── utils.py              # shared state, data models, helpers
├── requirements.txt      # Python packages
├── iris_offline.service  # systemd service for auto-start on boot
├── setup.sh              # full install + re-setup script (idempotent)
└── update.sh             # quick update: git pull → sync → restart
```

---

## Setup

> Run all commands below directly in your Raspberry Pi terminal.
> Connect via SSH, TigerVNC, or a keyboard+monitor.

---

### Step 1 — Flash the OS

Use **Raspberry Pi Imager** on your PC:
- OS: **Raspberry Pi OS Lite 64-bit (Bookworm)**
- Enable SSH in Imager settings (Ctrl+Shift+X) so you can log in remotely

---

### Step 2 — Enable I2C and Camera

```bash
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_camera 0
sudo reboot
```

---

### Step 3 — Verify Hardware

```bash
sudo i2cdetect -y 1            # MPU9250 should show as 68 (or 69)
libcamera-hello --list-cameras # Pi camera should be listed
```

---

### Step 4 — Run the Setup Script

Clone the repo and run `setup.sh` in one go:

```bash
sudo apt install -y git
git clone https://github.com/thekaushal01/Offline-Module-v2-for-IRIS2.0.git ~/iris_offline_src
sudo bash ~/iris_offline_src/iris_offline/setup.sh https://github.com/thekaushal01/Offline-Module-v2-for-IRIS2.0.git
```

The script handles everything automatically:
- Installs all system and Python dependencies
- Enables I2C, camera, GPU memory in `config.txt`
- Creates the `iris` system user
- Sets up a Python virtual environment
- Downloads the YOLOv8n model
- Generates a self-signed TLS certificate
- Installs and enables the systemd service
- Applies Pi 5 performance tuning

> The first run takes 10–15 minutes (mostly pip + model download). Subsequent re-runs take under a minute.

When the script finishes, reboot to activate I2C and camera changes:

```bash
sudo reboot
```

After reboot, IRIS starts automatically. Check it:

```bash
sudo systemctl status iris_offline
sudo journalctl -u iris_offline -f
```

---

## Updating After Code Changes

Whenever you edit code on your PC, commit, and push — run this single command on the Pi:

```bash
sudo bash /opt/iris_offline/update.sh
```

This does: `git pull` → sync changed files → pip install (if needed) → restart service. Takes under 30 seconds.

**What is preserved across updates:**
- TLS certificate (`cert.pem` / `key.pem`) — never regenerated unless expired
- YOLOv8n model (`yolov8n.pt`) — never re-downloaded
- Python virtual environment — only updated if `requirements.txt` changed

**Typical workflow:**

```
Your PC (VS Code)              Raspberry Pi
─────────────────              ─────────────────────────────────────
Edit code
git add .
git commit -m "fix: ..."
git push
                     ──────→  sudo bash /opt/iris_offline/update.sh
                               ✓ pulled 3 files
                               ✓ service restarted
                               ✓ IRIS running latest code
```

If you ever need to fully re-install from scratch (new Pi, or something broken):

```bash
sudo bash /opt/iris_offline/setup.sh
```

The same `setup.sh` does both full setup and re-setup. It skips steps that are already done (existing cert, existing user, etc.).

---

## JSON API Reference

**Main endpoint:** `GET https://<pi_ip>:5000/vision`

```json
{
  "timestamp": "2026-02-25T10:30:45.123+00:00",
  "vision": [
    { "name": "chair",  "confidence": 0.91 },
    { "name": "person", "confidence": 0.87 }
  ],
  "distance_feet": 2.4,
  "fall_detection": {
    "status": "normal",
    "impact_g": 1.02
  },
  "system_status": "warning",
  "fps": 10.2,
  "errors": []
}
```

**`system_status` values:**

| Value | When |
|---|---|
| `clear` | Everything normal |
| `warning` | Obstacle closer than 3 feet |
| `emergency` | Fall detected |

**Other endpoints:**

| Endpoint | What it returns |
|---|---|
| `GET /health` | Quick liveness check with uptime and FPS |
| `GET /stream` | MJPEG live video — open in any browser |

---

## Fall Detection Logic

The MPU9250 is polled at 50 Hz. Falls are detected in three phases:

```
NORMAL
  │  Total acceleration >= 2.5g (sudden impact)
  ▼
IMPACT_DETECTED
  │  Wait 0.5s for motion to settle
  ▼
IMMOBILITY_CHECK
  │  Acceleration variance < 0.05g for 3 consecutive seconds
  ▼
POSSIBLE_FALL  →  auto-clears after 5 seconds
```

If the person moves again before the 3-second immobility window completes, the state resets to `normal` — so bumping a table won't trigger a false alarm.

---

## Performance

| What | Target |
|---|---|
| Inference speed | 8–12 FPS at 640×480 |
| Ultrasonic polling | 10 Hz with 5-sample median filter |
| Fall detection | 50 Hz |
| JSON response time | < 50ms |
| RAM usage | < 500 MB total |
| Continuous uptime | 8+ hours |

---

## Thermal Tips

The Pi 5 will throttle at 85°C. For 8-hour sessions:

- Attach the **official RPi 5 active cooler** (highly recommended)
- Use a **vented case**, not a sealed enclosure
- Use the **official 27W USB-C PSU** — underpowering causes both throttling and instability
- Keep ambient temperature below 35°C

Monitor temperature while running:
```bash
watch -n2 vcgencmd measure_temp
vcgencmd get_throttled   # 0x0 means no throttle has occurred
```

---

## Troubleshooting

### MPU9250 not showing on I2C scan
```bash
sudo i2cdetect -y 1          # check for 0x68 or 0x69
dmesg | grep i2c             # look for errors
sudo modprobe i2c-dev        # force-load the I2C module
# Double-check: VCC = 3.3V (not 5V), AD0 tied to GND
```

### Camera not found
```bash
libcamera-hello --list-cameras   # should list at least one camera
python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"
# If nothing shows: check ribbon cable is firmly seated in CAM0 port
# Make sure camera is enabled: sudo raspi-config → Interface Options → Camera
```

### HC-SR04 giving wrong values
```bash
# Most common cause: missing voltage divider on ECHO pin
# ECHO outputs 5V — the Pi GPIO will be damaged without the divider
gpio -g read 24   # should read 0 at rest
```

### HTTPS API not reachable
```bash
curl -k https://localhost:5000/vision   # test locally first
ss -tlnp | grep 5000                    # confirm port is open
sudo journalctl -u iris_offline -f      # check for Flask errors
# On mobile app: install cert.pem as a trusted CA to avoid SSL errors
```

### Low FPS
```bash
vcgencmd get_throttled          # 0x0 = fine, anything else = problem
vcgencmd measure_temp           # check for heat throttle
top -H -p $(pgrep -f main.py)  # see per-thread CPU usage
# To reduce load: increase SKIP_FRAMES in vision.py from 1 to 2 or 3
```

### Check memory usage
```bash
watch -n5 "ps aux --sort=-%mem | head -8"
cat /proc/$(pgrep -f main.py)/status | grep VmRSS
```

---

## YOLO Model Options

You can swap `yolov8n.pt` for a larger model if you need better accuracy:

| Model | File size | FPS on Pi 5 | |
|---|---|---|---|
| yolov8n | 6.3 MB | 8–12 FPS | **Use this one** |
| yolov8s | 22 MB | 4–6 FPS | more accurate, slower |
| yolov8m | 52 MB | 1–3 FPS | too slow for real-time |

---

## License
MIT — Internal deployment only. Not for redistribution.
