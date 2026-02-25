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
- ArduCam 8MP (CSI ribbon to CAM0 port)
- HC-SR04 Ultrasonic Sensor
- MPU9250 IMU breakout board
- 1kΩ and 2kΩ resistors (for HC-SR04 ECHO voltage divider)
- Official 27W USB-C PSU

---

## Wiring

### HC-SR04 — Ultrasonic Distance Sensor

| HC-SR04 | Pi Pin | BCM | Note |
|---------|--------|-----|------|
| VCC | Pin 2 | — | 5V |
| GND | Pin 6 | — | Ground |
| TRIG | Pin 16 | GPIO23 | Output — 3.3V safe |
| ECHO | Pin 18 | GPIO24 | **Must use voltage divider** |

The HC-SR04 ECHO pin outputs 5V. The Pi GPIO only tolerates 3.3V.
Build this simple voltage divider between ECHO and GPIO24:

```
HC-SR04 ECHO ─── 1kΩ ─── GPIO24 (Pin 18)
                               │
                              2kΩ
                               │
                             GND
```

### MPU9250 — IMU (I2C)

| MPU9250 | Pi Pin | BCM | Note |
|---------|--------|-----|------|
| VCC | Pin 1 | — | 3.3V only — do not use 5V |
| GND | Pin 6 | — | Ground |
| SDA | Pin 3 | GPIO2 | I2C data |
| SCL | Pin 5 | GPIO3 | I2C clock |
| AD0 | GND | — | Sets I2C address to 0x68 |

### ArduCam 8MP

Connect via CSI-2 ribbon cable to the **CAM0** port on the Pi 5. No GPIO needed.

---

## Project Files

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
└── setup.sh              # automated setup script
```

---

## Complete Setup — Step by Step (Raspberry Pi Terminal)

> Run all commands below directly in your Raspberry Pi terminal.
> You must be connected (via TigerVNC, SSH, or keyboard) to run these.

---

### Step 1 — Flash the OS

Use **Raspberry Pi Imager** on your PC:
- OS: **Raspberry Pi OS Lite 64-bit (Bookworm)**
- Enable SSH in Imager settings so you can log in remotely

---

### Step 2 — First Boot: Update the System

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot
```

---

### Step 3 — Enable I2C and Camera

```bash
sudo raspi-config
```

Inside raspi-config:
- **Interface Options → I2C → Enable**
- **Interface Options → Camera → Enable**
- **Finish → Reboot when asked**

Or enable them directly without the menu:

```bash
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_camera 0
sudo reboot
```

---

### Step 4 — Verify Hardware is Detected

**Check MPU9250 on I2C bus:**
```bash
sudo apt install -y i2c-tools
sudo i2cdetect -y 1
```
You should see `68` in the grid (or `69` if ADO is connected to 3.3V instead of GND).

**Check ArduCam:**
```bash
libcamera-hello --list-cameras
```
You should see at least one camera listed.

**Quick test photo:**
```bash
libcamera-jpeg -o ~/test.jpg
ls -lh ~/test.jpg   # should be a non-empty file
```

---

### Step 5 — Install System Dependencies

```bash
sudo apt install -y \
    python3 python3-pip python3-venv python3-dev \
    python3-opencv libopencv-dev \
    libatlas-base-dev libhdf5-dev \
    libopenblas-dev libblas-dev liblapack-dev \
    libjpeg-dev libpng-dev \
    libcamera-dev libcamera-apps \
    v4l-utils openssl git htop
```

---

### Step 6 — Clone the Project

```bash
git clone <your_repo_url> ~/iris_offline
cd ~/iris_offline
```

---

### Step 7 — Create Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

---

### Step 8 — Install Python Packages

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs: `ultralytics` (YOLOv8), `opencv-python-headless`, `flask`, `smbus2`, `RPi.GPIO`, and others.

> The first install may take 5–10 minutes on the Pi. This is normal.

---

### Step 9 — Download the YOLOv8n Model

```bash
python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
```

This downloads `yolov8n.pt` (~6MB) into the current directory. Only needed once.

---

### Step 10 — Generate TLS Certificate

The HTTPS server needs a certificate. Run this once:

```bash
PI_IP=$(hostname -I | awk '{print $1}')

openssl req -x509 -newkey rsa:4096 -sha256 -days 1095 \
    -nodes \
    -keyout key.pem \
    -out cert.pem \
    -subj "/CN=iris-offline/O=IRIS2/C=US" \
    -addext "subjectAltName=IP:${PI_IP},IP:127.0.0.1,DNS:localhost"

echo "Certificate generated for IP: ${PI_IP}"
```

---

### Step 11 — Run IRIS

```bash
cd ~/iris_offline
source venv/bin/activate
python main.py
```

The dashboard window will appear on the display.
The JSON API will be live at:

```
https://<raspberry_pi_ip>:5000/vision
```

Test it from another terminal:
```bash
curl -k https://localhost:5000/vision
curl -k https://localhost:5000/health
```

---

### Step 12 — (Optional) Auto-start on Boot with systemd

Do this when you're ready to deploy and want IRIS to start automatically every time the Pi powers on:

```bash
# Copy the service file
sudo cp ~/iris_offline/iris_offline.service /etc/systemd/system/

# Reload systemd and enable the service
sudo systemctl daemon-reload
sudo systemctl enable iris_offline
sudo systemctl start iris_offline

# Check it's running
sudo systemctl status iris_offline

# Watch live logs
sudo journalctl -u iris_offline -f
```

To stop or restart:
```bash
sudo systemctl stop iris_offline
sudo systemctl restart iris_offline
```

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
ls /dev/video*                   # check device node exists
v4l2-ctl --list-devices          # alternative check
# If GStreamer fails in vision.py: set use_gstreamer=False in CameraSource
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
