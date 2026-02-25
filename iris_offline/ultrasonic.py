"""
ultrasonic.py — HC-SR04 distance measurement thread
IRIS 2.0 Offline Module

Wiring:
  HC-SR04 VCC   → Pi 5V (Pin 2)
  HC-SR04 GND   → Pi GND (Pin 6)
  HC-SR04 TRIG  → GPIO23 (Pin 16)  [configurable]
  HC-SR04 ECHO  → GPIO24 (Pin 18) via 1kΩ/2kΩ voltage divider  [5V→3.3V]

IMPORTANT: RPi GPIO is 3.3V. HC-SR04 ECHO returns 5V.
Use a voltage divider: ECHO → 1kΩ → GPIO24 → 2kΩ → GND
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("iris.ultrasonic")

# ---------------------------------------------------------------------------
# GPIO import — graceful degradation on non-Pi systems
# ---------------------------------------------------------------------------
try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    log.warning("RPi.GPIO not available — ultrasonic will use simulated distance")

from utils import SharedState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GPIO_TRIG       = 23          # BCM pin numbers
GPIO_ECHO       = 24
SPEED_OF_SOUND  = 34300.0     # cm/s at ~20°C
CM_TO_FEET      = 0.0328084
MAX_DISTANCE_CM = 400.0       # HC-SR04 rated range
MIN_DISTANCE_CM = 2.0
PULSE_TIMEOUT   = 0.03        # 30 ms timeout (~5m max travel time)
MEASUREMENT_INTERVAL = 0.10   # 10 Hz polling
MEDIAN_SAMPLES  = 5           # Median filter size

# ---------------------------------------------------------------------------
# Low-level HC-SR04 driver
# ---------------------------------------------------------------------------

class HCSR04Driver:
    """
    Direct GPIO driver for HC-SR04.
    All timing in seconds; converts to cm, then feet.
    """

    def __init__(self, trig_pin: int = GPIO_TRIG, echo_pin: int = GPIO_ECHO) -> None:
        self._trig = trig_pin
        self._echo = echo_pin
        self._ready = False

    def setup(self) -> bool:
        if not _GPIO_AVAILABLE:
            return False
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self._trig, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(self._echo, GPIO.IN)
            time.sleep(0.05)   # Sensor stabilise
            self._ready = True
            log.info("HC-SR04 GPIO initialised (TRIG=%d, ECHO=%d)", self._trig, self._echo)
            return True
        except Exception as exc:
            log.error("HC-SR04 setup failed: %s", exc)
            return False

    def measure_cm(self) -> Optional[float]:
        """
        Fire one pulse and return distance in cm.
        Returns None on timeout or hardware error.
        """
        if not self._ready:
            return None

        try:
            # Send 10 µs trigger pulse
            GPIO.output(self._trig, GPIO.HIGH)
            time.sleep(0.00001)
            GPIO.output(self._trig, GPIO.LOW)

            # Wait for ECHO to go HIGH (start of return pulse)
            start_wait = time.perf_counter()
            while GPIO.input(self._echo) == GPIO.LOW:
                if time.perf_counter() - start_wait > PULSE_TIMEOUT:
                    log.debug("HC-SR04: ECHO HIGH timeout")
                    return None
            pulse_start = time.perf_counter()

            # Wait for ECHO to go LOW (end of return pulse)
            while GPIO.input(self._echo) == GPIO.HIGH:
                if time.perf_counter() - pulse_start > PULSE_TIMEOUT:
                    log.debug("HC-SR04: ECHO LOW timeout")
                    return None
            pulse_end = time.perf_counter()

            duration = pulse_end - pulse_start
            distance_cm = (duration * SPEED_OF_SOUND) / 2.0

            if MIN_DISTANCE_CM <= distance_cm <= MAX_DISTANCE_CM:
                return distance_cm
            return None

        except Exception as exc:
            log.warning("HC-SR04 measure error: %s", exc)
            return None

    def cleanup(self) -> None:
        if _GPIO_AVAILABLE and self._ready:
            try:
                GPIO.cleanup([self._trig, self._echo])
            except Exception:
                pass
            self._ready = False


# ---------------------------------------------------------------------------
# Median filter for stable readings
# ---------------------------------------------------------------------------

class MedianFilter:
    def __init__(self, size: int = MEDIAN_SAMPLES) -> None:
        self._size = size
        self._buf: list = []

    def push(self, value: float) -> Optional[float]:
        self._buf.append(value)
        if len(self._buf) > self._size:
            self._buf.pop(0)
        if len(self._buf) == self._size:
            sorted_buf = sorted(self._buf)
            return sorted_buf[self._size // 2]
        return None


# ---------------------------------------------------------------------------
# Sensor thread
# ---------------------------------------------------------------------------

class UltrasonicThread(threading.Thread):
    """
    Background 10 Hz polling thread for HC-SR04.
    Applies median filter and pushes distance_feet into SharedState.
    Automatically retries GPIO setup on failure.
    """

    _RETRY_INTERVAL  = 5.0    # seconds between reinit attempts
    _SIM_DISTANCE_FT = 5.0    # simulated distance when GPIO unavailable

    def __init__(self, state: SharedState) -> None:
        super().__init__(name="UltrasonicThread", daemon=True)
        self._state  = state
        self._driver = HCSR04Driver()
        self._filter = MedianFilter()

    def run(self) -> None:
        log.info("UltrasonicThread starting")
        self._init_with_retry()

        consecutive_failures = 0
        MAX_FAILURES = 20   # ~2 s before reinit attempt

        while not self._state.is_shutdown_requested():
            t0 = time.perf_counter()

            if not _GPIO_AVAILABLE:
                # Simulation mode — oscillate distance for testing
                import math
                sim = self._SIM_DISTANCE_FT + math.sin(time.time() * 0.5)
                self._state.update_distance(round(sim, 2))
                time.sleep(MEASUREMENT_INTERVAL)
                continue

            cm = self._driver.measure_cm()

            if cm is not None:
                consecutive_failures = 0
                filtered = self._filter.push(cm)
                if filtered is not None:
                    feet = filtered * CM_TO_FEET
                    self._state.update_distance(feet)
            else:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    log.warning("HC-SR04 consecutive failures — reinitialising GPIO")
                    self._state.add_error("HC-SR04 measurement failures, reinit")
                    self._driver.cleanup()
                    time.sleep(self._RETRY_INTERVAL)
                    self._init_with_retry()
                    consecutive_failures = 0

            # Honour desired polling rate accounting for measurement time
            elapsed = time.perf_counter() - t0
            sleep_time = max(0.0, MEASUREMENT_INTERVAL - elapsed)
            time.sleep(sleep_time)

        self._driver.cleanup()
        log.info("UltrasonicThread stopped")

    def _init_with_retry(self) -> None:
        while not self._state.is_shutdown_requested():
            if self._driver.setup():
                return
            self._state.add_error("HC-SR04 GPIO init failed, retrying")
            time.sleep(self._RETRY_INTERVAL)
