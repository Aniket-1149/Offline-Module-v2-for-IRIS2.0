"""
fall_detection.py — MPU9250 I2C fall detection thread
IRIS 2.0 Offline Module

MPU9250 Wiring:
  MPU9250 VCC  → Pi 3.3V (Pin 1)
  MPU9250 GND  → Pi GND  (Pin 6)
  MPU9250 SDA  → Pi SDA1 (Pin 3 / GPIO2)
  MPU9250 SCL  → Pi SCL1 (Pin 5 / GPIO3)
  MPU9250 AD0  → GND     → I2C address 0x68
                OR 3.3V  → I2C address 0x69

Enable I2C on Pi:
  sudo raspi-config → Interface Options → I2C → Enable
  sudo i2cdetect -y 1   # Verify 0x68 appears
"""

import logging
import math
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger("iris.fall")

# ---------------------------------------------------------------------------
# SMBus / I2C import
# ---------------------------------------------------------------------------
try:
    import smbus2
    _I2C_AVAILABLE = True
except ImportError:
    _I2C_AVAILABLE = False
    log.warning("smbus2 not available — fall detection will use simulation")

from utils import SharedState, FallState

# ---------------------------------------------------------------------------
# MPU9250 Register Map
# ---------------------------------------------------------------------------
MPU9250_ADDR       = 0x68    # AD0 LOW; use 0x69 if AD0 HIGH

# Power management
PWR_MGMT_1         = 0x6B    # Wake device: write 0x00
PWR_MGMT_2         = 0x6C

# Configuration
CONFIG             = 0x1A    # DLPF config
GYRO_CONFIG        = 0x1B    # Gyro full-scale
ACCEL_CONFIG       = 0x1C    # Accel full-scale ±2g = 0x00, ±4g = 0x08, ±8g = 0x10, ±16g = 0x18
ACCEL_CONFIG2      = 0x1D

# Interrupt
INT_PIN_CFG        = 0x37
INT_ENABLE         = 0x38
INT_STATUS         = 0x3A

# Accel data (high byte first)
ACCEL_XOUT_H       = 0x3B    # 6 bytes: AX_H AX_L AY_H AY_L AZ_H AZ_L

# Temperature
TEMP_OUT_H         = 0x41    # 2 bytes

# Gyro data
GYRO_XOUT_H        = 0x43    # 6 bytes

# Device identity
WHO_AM_I           = 0x75    # Should return 0x71 (MPU9250) or 0x70 (MPU9255)

# Scale factors
ACCEL_SCALE_2G     = 16384.0   # LSB/g at ±2g
ACCEL_SCALE_4G     = 8192.0
ACCEL_SCALE_8G     = 4096.0
ACCEL_SCALE_16G    = 2048.0

# ---------------------------------------------------------------------------
# Fall detection thresholds
# ---------------------------------------------------------------------------
IMPACT_G_THRESHOLD     = 2.5    # Sudden acceleration > 2.5g → impact
FREEFALL_G_THRESHOLD   = 0.4    # Total accel < 0.4g → freefall window
IMMOBILITY_G_VARIANCE  = 0.05   # Variance < this for N seconds → immobility
IMMOBILITY_SECONDS     = 3.0    # Must be still this long after impact
FALL_COOLDOWN_SECONDS  = 5.0    # Minimum time between fall events

POLL_RATE_HZ           = 50     # 50 Hz → 20 ms per sample
POLL_INTERVAL          = 1.0 / POLL_RATE_HZ

# ---------------------------------------------------------------------------
# MPU9250 Driver (raw I2C register access via smbus2)
# ---------------------------------------------------------------------------

class MPU9250Driver:
    """
    Direct I2C register communication with MPU9250.
    No third-party MPU library required — all register ops are explicit.
    """

    def __init__(self, bus_id: int = 1, address: int = MPU9250_ADDR) -> None:
        self._bus_id    = bus_id
        self._address   = address
        self._bus: Optional[smbus2.SMBus] = None
        self._scale     = ACCEL_SCALE_2G   # default ±2g
        self._ready     = False

    def open(self) -> bool:
        if not _I2C_AVAILABLE:
            return False
        try:
            self._bus = smbus2.SMBus(self._bus_id)
            time.sleep(0.1)

            # Verify device identity
            who = self._read_byte(WHO_AM_I)
            if who not in (0x71, 0x70, 0x68):  # MPU9250 / MPU9255 / MPU6050
                log.error("MPU9250 WHO_AM_I = 0x%02X (expected 0x71/0x70)", who)
                return False

            self._configure()
            self._ready = True
            log.info("MPU9250 on bus %d addr 0x%02X ready (WHO_AM_I=0x%02X)",
                     self._bus_id, self._address, who)
            return True

        except Exception as exc:
            log.error("MPU9250 open failed: %s", exc)
            if self._bus:
                self._bus.close()
                self._bus = None
            return False

    def _configure(self) -> None:
        # Wake device and select internal 20 MHz oscillator
        self._write_byte(PWR_MGMT_1, 0x00)
        time.sleep(0.1)

        # Set clock source to PLL gyro X (better accuracy)
        self._write_byte(PWR_MGMT_1, 0x01)

        # Disable sleep, enable all axes
        self._write_byte(PWR_MGMT_2, 0x00)

        # DLPF bandwidth ~41 Hz accel, 42 Hz gyro → smooth without too much lag
        self._write_byte(CONFIG, 0x03)

        # Accel config: ±2g full scale (most sensitive for fall detection)
        self._write_byte(ACCEL_CONFIG, 0x00)
        self._scale = ACCEL_SCALE_2G

        # Accel DLPF: 44.8 Hz
        self._write_byte(ACCEL_CONFIG2, 0x03)

        # Gyro full-scale: ±250 °/s
        self._write_byte(GYRO_CONFIG, 0x00)

        log.debug("MPU9250 configured")

    def read_accel_g(self) -> Tuple[float, float, float]:
        """
        Return (ax, ay, az) in g-force units.
        Reads 6 consecutive registers starting at ACCEL_XOUT_H.
        """
        raw = self._read_bytes(ACCEL_XOUT_H, 6)
        ax = self._to_signed16(raw[0], raw[1]) / self._scale
        ay = self._to_signed16(raw[2], raw[3]) / self._scale
        az = self._to_signed16(raw[4], raw[5]) / self._scale
        return ax, ay, az

    def read_temperature_c(self) -> float:
        """Read die temperature in °C."""
        raw = self._read_bytes(TEMP_OUT_H, 2)
        raw_temp = self._to_signed16(raw[0], raw[1])
        return (raw_temp / 333.87) + 21.0

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None
        self._ready = False

    # -- Private helpers -------------------------------------------------------

    def _write_byte(self, register: int, value: int) -> None:
        self._bus.write_byte_data(self._address, register, value)

    def _read_byte(self, register: int) -> int:
        return self._bus.read_byte_data(self._address, register)

    def _read_bytes(self, register: int, length: int) -> bytes:
        return self._bus.read_i2c_block_data(self._address, register, length)

    @staticmethod
    def _to_signed16(high: int, low: int) -> int:
        """Combine two bytes into a signed 16-bit integer (big-endian)."""
        val = (high << 8) | low
        return val - 65536 if val >= 32768 else val

    @property
    def is_ready(self) -> bool:
        return self._ready


# ---------------------------------------------------------------------------
# Fall detection state machine
# ---------------------------------------------------------------------------

class FallStateMachine:
    """
    3-phase fall detection:
      NORMAL → FREEFALL/IMPACT → IMMOBILITY_CHECK → FALL_CONFIRMED
    """

    _STATE_NORMAL    = "normal"
    _STATE_IMPACT    = "impact_pending"
    _STATE_IMMOBILE  = "immobility_check"

    def __init__(self) -> None:
        self._state           = self._STATE_NORMAL
        self._impact_time     = 0.0
        self._last_fall_time  = 0.0
        self._accel_history   = []   # recent accel magnitudes for variance
        self._result          = FallState()

    def update(self, ax: float, ay: float, az: float) -> FallState:
        now  = time.time()
        mag  = math.sqrt(ax**2 + ay**2 + az**2)   # total G magnitude

        # Rolling buffer for immobility variance (last 1 second @ 50 Hz = 50 samples)
        self._accel_history.append(mag)
        if len(self._accel_history) > 50:
            self._accel_history.pop(0)

        # Cooldown — prevent rapid repeated alerts
        if now - self._last_fall_time < FALL_COOLDOWN_SECONDS:
            return self._result

        if self._state == self._STATE_NORMAL:
            # Detect sudden impact spike
            if mag >= IMPACT_G_THRESHOLD:
                log.info("Impact detected: %.2fg", mag)
                self._state       = self._STATE_IMPACT
                self._impact_time = now
                self._result = FallState(
                    status="impact_detected",
                    impact_g=round(mag, 3),
                    last_update=now,
                )

        elif self._state == self._STATE_IMPACT:
            # After impact, watch for immobility (person has fallen and isn't moving)
            elapsed = now - self._impact_time
            if elapsed > 0.5:   # Wait 0.5 s for ringing to settle
                self._state = self._STATE_IMMOBILE

        elif self._state == self._STATE_IMMOBILE:
            if len(self._accel_history) >= 10:
                variance = _variance(self._accel_history[-25:])   # last 0.5 s
                if variance < IMMOBILITY_G_VARIANCE:
                    immobile_duration = now - self._impact_time
                    if immobile_duration >= IMMOBILITY_SECONDS:
                        log.warning("Fall confirmed — immobility %.1f s variance=%.4f",
                                    immobile_duration, variance)
                        self._last_fall_time = now
                        self._state = self._STATE_NORMAL
                        self._result = FallState(
                            status="possible_fall",
                            impact_g=self._result.impact_g,
                            last_update=now,
                        )
                else:
                    # Person is moving — not a fall
                    log.info("Person recovered — resetting fall state")
                    self._state = self._STATE_NORMAL
                    self._result = FallState(status="normal", last_update=now)

        # Auto-clear fall status after cooldown
        if (self._result.status != "normal"
                and now - self._result.last_update > FALL_COOLDOWN_SECONDS):
            self._result = FallState(status="normal", last_update=now)

        self._result.impact_g = round(mag, 3)
        return self._result


def _variance(data: list) -> float:
    if len(data) < 2:
        return 0.0
    mean = sum(data) / len(data)
    return sum((x - mean) ** 2 for x in data) / len(data)


# ---------------------------------------------------------------------------
# Fall detection thread
# ---------------------------------------------------------------------------

class FallDetectionThread(threading.Thread):
    """
    50 Hz I2C polling thread.
    Runs fall state machine and pushes FallState into SharedState.
    Auto-reconnects on I2C errors.
    """

    _RETRY_INTERVAL = 5.0

    def __init__(self, state: SharedState, bus_id: int = 1) -> None:
        super().__init__(name="FallDetectionThread", daemon=True)
        self._state   = state
        self._driver  = MPU9250Driver(bus_id=bus_id)
        self._machine = FallStateMachine()

    def run(self) -> None:
        log.info("FallDetectionThread starting")
        self._init_with_retry()

        consecutive_errors = 0
        MAX_ERRORS = 50   # ~1 second before reinit

        while not self._state.is_shutdown_requested():
            t0 = time.perf_counter()

            if not _I2C_AVAILABLE:
                # Simulate gentle vibration + occasional spike
                import random
                ax = random.gauss(0.0, 0.02)
                ay = random.gauss(0.0, 0.02)
                az = random.gauss(1.0, 0.02)  # ~1g resting
                fall_state = self._machine.update(ax, ay, az)
                self._state.update_fall(fall_state)
                time.sleep(POLL_INTERVAL)
                continue

            try:
                ax, ay, az = self._driver.read_accel_g()
                consecutive_errors = 0
                fall_state = self._machine.update(ax, ay, az)
                self._state.update_fall(fall_state)

            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors >= MAX_ERRORS:
                    log.error("MPU9250 I2C errors — reinitialising: %s", exc)
                    self._state.add_error(f"MPU9250 I2C error: {exc}")
                    self._driver.close()
                    time.sleep(self._RETRY_INTERVAL)
                    self._init_with_retry()
                    consecutive_errors = 0

            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, POLL_INTERVAL - elapsed))

        self._driver.close()
        log.info("FallDetectionThread stopped")

    def _init_with_retry(self) -> None:
        while not self._state.is_shutdown_requested():
            if self._driver.open():
                return
            self._state.add_error("MPU9250 init failed, retrying")
            time.sleep(self._RETRY_INTERVAL)
