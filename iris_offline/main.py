"""
main.py — IRIS 2.0 Offline Module entry point
Orchestrates all threads and handles graceful shutdown.
"""

import logging
import os
import signal
import subprocess
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


def _kill_previous_instance() -> None:
    """
    Kill any previous main.py process so it releases port 5000 before we start.
    Without this, running IRIS a second time always crashes with 'Address already in use'.
    """
    my_pid = os.getpid()
    killed = []
    try:
        result = subprocess.run(
            ["pgrep", "-f", "iris_offline/main.py"],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
                if pid != my_pid:
                    os.kill(pid, signal.SIGTERM)
                    killed.append(pid)
            except (ValueError, ProcessLookupError):
                pass
    except FileNotFoundError:
        pass  # pgrep not available
    if killed:
        log.info("Stopped previous IRIS process(es): %s — waiting for port release...", killed)
        time.sleep(2.0)


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

    _kill_previous_instance()

    state = _build_state()
    _register_signals(state)
    threads = _start_all_threads(state)

    log.info("All systems running. Press Ctrl+C or SIGTERM to stop.")
    log.info("JSON endpoint: https://localhost:5000/vision")
    log.info("Video stream:  https://localhost:5000/stream")

    # ── Live status display ────────────────────────────────────────────────
    # Prints a single overwriting line every 0.5 s so you can see detections
    # and sensor readings without flooding the terminal with new log lines.
    # A proper log.info heartbeat still fires every 30 s for the journal.
    STATUS_INTERVAL    = 0.5    # seconds between live-status refreshes
    LOG_INTERVAL       = 30.0   # seconds between log.info heartbeats
    STARTUP_CLEAR_DELAY = 12.0  # seconds after which transient init errors are cleared
    last_status = 0.0
    last_log    = time.time()
    startup_cleared = False

    try:
        while not state.is_shutdown_requested():
            time.sleep(0.1)   # tight loop so shutdown is responsive
            now = time.time()

            # ── clear transient startup errors once things have settled ───
            if not startup_cleared and now - last_log >= STARTUP_CLEAR_DELAY:
                state.clear_errors()
                startup_cleared = True

            # ── live terminal status (overwrites previous line) ────────────
            if now - last_status >= STATUS_INTERVAL:
                snap = state.snapshot()
                if snap.detections:
                    det_str = ", ".join(
                        f"{d.name}({d.confidence:.0%})"
                        for d in snap.detections[:6]
                    )
                else:
                    det_str = "nothing detected"
                status_line = (
                    f"  FPS={snap.fps:.1f}  "
                    f"dist={snap.distance_feet:.2f}ft  "
                    f"fall={snap.fall.status}  "
                    f"[{det_str}]  "
                    f"status={snap.system_status}"
                )
                print(f"\r{status_line:<130}", end="", flush=True)
                last_status = now

            # ── periodic log.info heartbeat (for systemd journal / file) ──
            if now - last_log >= LOG_INTERVAL:
                snap = state.snapshot()
                log.info(
                    "Heartbeat | FPS=%.1f | dist=%.2fft | fall=%s | status=%s | errors=%d",
                    snap.fps,
                    snap.distance_feet,
                    snap.fall.status,
                    snap.system_status,
                    len(snap.errors),
                )
                last_log = now

    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received")
        state.request_shutdown()

    log.info("Shutting down...")
    _join_threads(threads)
    log.info("IRIS 2.0 Offline Module stopped cleanly")


if __name__ == "__main__":
    main()
