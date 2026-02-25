"""
utils.py — Thread-safe shared state, logging, and system utilities
IRIS 2.0 Offline Module
"""

import threading
import logging
import time
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional, Any
import numpy as np


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE = "/var/log/iris_offline.log"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logger for both file and console output."""
    handlers: List[logging.Handler] = [logging.StreamHandler()]

    # Attempt file logging (may fail if running without root on Pi)
    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        handlers.append(fh)
    except PermissionError:
        pass  # Fall back to console only

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=handlers,
    )
    return logging.getLogger("iris")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """Single YOLO detection result."""
    name: str
    confidence: float
    bbox: Optional[List[float]] = None   # [x1, y1, x2, y2] in pixel coords


@dataclass
class FallState:
    """Fall detection snapshot."""
    status: str = "normal"          # normal | impact_detected | possible_fall
    impact_g: float = 1.0
    last_update: float = field(default_factory=time.time)


@dataclass
class SensorFrame:
    """Complete sensor snapshot passed between threads."""
    timestamp: str = ""
    detections: List[Detection] = field(default_factory=list)
    raw_frame: Optional[Any] = None          # numpy BGR frame for UI
    annotated_frame: Optional[Any] = None   # numpy BGR frame with boxes
    distance_feet: float = -1.0             # -1 = unavailable
    fall: FallState = field(default_factory=FallState)
    system_status: str = "clear"            # clear | warning | emergency
    fps: float = 0.0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Thread-safe shared state
# ---------------------------------------------------------------------------

class SharedState:
    """
    Central, thread-safe data bus that all modules read/write.
    Uses a single RLock so readers never see a partially updated frame.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._frame = SensorFrame()
        self._shutdown_event = threading.Event()

    # -- Write ---------------------------------------------------------------

    def update_vision(
        self,
        detections: List[Detection],
        fps: float,
        raw_frame: Any,
        annotated_frame: Any,
    ) -> None:
        with self._lock:
            self._frame.detections = detections
            self._frame.fps = round(fps, 1)
            self._frame.raw_frame = raw_frame
            self._frame.annotated_frame = annotated_frame
            self._frame.timestamp = _iso_now()
            self._recalculate_status()

    def update_distance(self, feet: float) -> None:
        with self._lock:
            self._frame.distance_feet = round(feet, 2)
            self._recalculate_status()

    def update_fall(self, state: FallState) -> None:
        with self._lock:
            self._frame.fall = state
            self._recalculate_status()

    def add_error(self, msg: str) -> None:
        with self._lock:
            # Keep last 10 errors to prevent unbounded growth
            self._frame.errors.append(msg)
            if len(self._frame.errors) > 10:
                self._frame.errors.pop(0)

    def clear_errors(self) -> None:
        with self._lock:
            self._frame.errors.clear()

    # -- Read ----------------------------------------------------------------

    def snapshot(self) -> SensorFrame:
        """Return a shallow copy of the current frame (safe to read off-thread)."""
        with self._lock:
            import copy
            return copy.copy(self._frame)

    def get_annotated_frame(self) -> Optional[Any]:
        with self._lock:
            return self._frame.annotated_frame

    # -- Shutdown ------------------------------------------------------------

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    # -- Internal ------------------------------------------------------------

    def _recalculate_status(self) -> None:
        """Must be called under self._lock."""
        if self._frame.fall.status in ("impact_detected", "possible_fall"):
            self._frame.system_status = "emergency"
        elif 0.0 < self._frame.distance_feet < 3.0:
            self._frame.system_status = "warning"
        else:
            self._frame.system_status = "clear"


# ---------------------------------------------------------------------------
# FPS counter
# ---------------------------------------------------------------------------

class FPSCounter:
    """Rolling-window FPS calculator (no dependency on time.perf_counter drift)."""

    def __init__(self, window: int = 30) -> None:
        self._window = window
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def tick(self) -> float:
        """Call once per frame. Returns current FPS."""
        now = time.perf_counter()
        with self._lock:
            self._timestamps.append(now)
            if len(self._timestamps) > self._window:
                self._timestamps.pop(0)
            if len(self._timestamps) < 2:
                return 0.0
            elapsed = self._timestamps[-1] - self._timestamps[0]
            return (len(self._timestamps) - 1) / elapsed if elapsed > 0 else 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def retry(max_attempts: int = 5, delay: float = 2.0, exceptions=(Exception,)):
    """
    Decorator: retry a function up to max_attempts times on failure.
    Useful for sensor init that may transiently fail on boot.
    """
    def decorator(fn):
        def wrapper(*args, **kwargs):
            log = logging.getLogger("iris.retry")
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    log.warning(
                        "Attempt %d/%d for %s failed: %s",
                        attempt, max_attempts, fn.__name__, exc,
                    )
                    if attempt < max_attempts:
                        time.sleep(delay)
            raise RuntimeError(f"{fn.__name__} failed after {max_attempts} attempts")
        return wrapper
    return decorator


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
