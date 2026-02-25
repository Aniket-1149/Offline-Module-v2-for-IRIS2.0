"""
main.py — IRIS 2.0 Offline Module entry point
Orchestrates all threads and handles graceful shutdown.
"""

import logging
import os
import signal
import sys
import time

# Ensure module-local imports resolve correctly when run as a script
sys.path.insert(0, os.path.dirname(__file__))

from utils import SharedState, configure_logging
from vision import VisionThread
from ultrasonic import UltrasonicThread
from fall_detection import FallDetectionThread
from server import ServerThread
from ui import UIThread

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

log = configure_logging(logging.INFO)


def _build_state() -> SharedState:
    return SharedState()


def _register_signals(state: SharedState) -> None:
    """Register SIGINT / SIGTERM for clean shutdown."""
    def _handler(signum, frame):
        log.info("Signal %d received — requesting shutdown", signum)
        state.request_shutdown()

    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)


def _start_all_threads(state: SharedState):
    threads = []

    # Sensor threads (daemon — will die with main)
    for cls in (UltrasonicThread, FallDetectionThread):
        t = cls(state)
        t.start()
        threads.append(t)
        log.info("Started %s", t.name)

    # Vision thread (camera + YOLO)
    vt = VisionThread(state)
    vt.start()
    threads.append(vt)
    log.info("Started %s", vt.name)

    # HTTPS server thread
    st = ServerThread(state, host="0.0.0.0", port=5000)
    st.start()
    threads.append(st)
    log.info("Started %s", st.name)

    # UI thread — must start last (some OS require window from main thread;
    # on RPi with X11/Wayland this is fine in a thread)
    ut = UIThread(state)
    ut.start()
    threads.append(ut)
    log.info("Started %s", ut.name)

    return threads


def _join_threads(threads, timeout: float = 5.0) -> None:
    log.info("Joining threads (timeout=%.1fs each)...", timeout)
    for t in threads:
        t.join(timeout=timeout)
        if t.is_alive():
            log.warning("%s did not stop cleanly", t.name)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("IRIS 2.0 Offline Module — Starting up")
    log.info("=" * 60)

    state = _build_state()
    _register_signals(state)
    threads = _start_all_threads(state)

    log.info("All systems running. Press Ctrl+C or SIGTERM to stop.")
    log.info("JSON endpoint: https://localhost:5000/vision")
    log.info("Video stream:  https://localhost:5000/stream")

    # Heartbeat loop — keep main thread alive and log periodic status
    HEARTBEAT_INTERVAL = 30.0   # seconds
    last_heartbeat = time.time()

    try:
        while not state.is_shutdown_requested():
            time.sleep(0.5)

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                snap = state.snapshot()
                log.info(
                    "Heartbeat | FPS=%.1f | dist=%.2fft | fall=%s | status=%s | errors=%d",
                    snap.fps,
                    snap.distance_feet,
                    snap.fall.status,
                    snap.system_status,
                    len(snap.errors),
                )
                last_heartbeat = time.time()

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received")
        state.request_shutdown()

    log.info("Shutting down...")
    _join_threads(threads)
    log.info("IRIS 2.0 Offline Module stopped cleanly")


if __name__ == "__main__":
    main()
