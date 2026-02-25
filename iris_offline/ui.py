"""
ui.py — Local OpenCV dashboard UI thread
IRIS 2.0 Offline Module

Displays on the Raspberry Pi's connected HDMI/DSI display.
Uses OpenCV imshow() in a dedicated thread — lightweight, no Qt dependency.

Layout (640×600 composite window):
  ┌─────────────────────────────────────────────────────────────┐
  │  [LIVE CAMERA + YOLO BOXES  640×480]                        │
  ├──────────────┬──────────────┬───────────────────────────────┤
  │  OBJECT LIST │  DISTANCE    │  FALL STATUS  │  SYS STATUS  │
  │              │              │               │  FPS          │
  └──────────────┴──────────────┴───────────────┴───────────────┘
"""

import logging
import threading
import time
from typing import List, Optional

import cv2
import numpy as np

from utils import SharedState, SensorFrame

log = logging.getLogger("iris.ui")

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
CAM_W, CAM_H     = 640, 480
BAR_H            = 120          # Height of info bar below camera
WIN_W, WIN_H     = CAM_W, CAM_H + BAR_H    # 640 × 600

FONT      = cv2.FONT_HERSHEY_SIMPLEX
FONT_BOLD = cv2.FONT_HERSHEY_DUPLEX

# Colour palette (BGR)
C_WHITE    = (255, 255, 255)
C_BLACK    = (0,   0,   0)
C_GREEN    = (0,   210, 90)
C_YELLOW   = (0,   220, 255)
C_RED      = (30,  30,  230)
C_ORANGE   = (0,   140, 255)
C_CYAN     = (230, 200, 40)
C_DARK     = (28,  28,  28)
C_PANEL    = (45,  45,  45)
C_ACCENT   = (255, 140, 0)

STATUS_COLOURS = {
    "clear":     C_GREEN,
    "warning":   C_YELLOW,
    "emergency": C_RED,
}

FALL_COLOURS = {
    "normal":           C_GREEN,
    "impact_detected":  C_ORANGE,
    "possible_fall":    C_RED,
}

WINDOW_NAME = "IRIS 2.0 — Offline AI Module"

# ---------------------------------------------------------------------------
# Dashboard composer
# ---------------------------------------------------------------------------

class Dashboard:
    """Renders a single composite frame from a SensorFrame snapshot."""

    def __init__(self) -> None:
        self._canvas = np.zeros((WIN_H, WIN_W, 3), dtype=np.uint8)
        self._no_signal = self._make_no_signal()

    # -- Public entry point --------------------------------------------------

    def render(self, snap: SensorFrame) -> np.ndarray:
        canvas = self._canvas.copy()
        canvas[:] = C_DARK

        # Top: camera feed with YOLO boxes
        cam_frame = snap.annotated_frame
        if cam_frame is not None:
            try:
                resized = cv2.resize(cam_frame, (CAM_W, CAM_H))
                canvas[0:CAM_H, 0:CAM_W] = resized
            except Exception:
                canvas[0:CAM_H, 0:CAM_W] = self._no_signal
        else:
            canvas[0:CAM_H, 0:CAM_W] = self._no_signal

        # Bottom info bar
        bar = self._render_info_bar(snap)
        canvas[CAM_H:WIN_H, 0:WIN_W] = bar

        # Overlay FPS on top-left of camera area
        self._draw_fps_badge(canvas, snap.fps)

        # Overlay timestamp top-right
        self._draw_timestamp(canvas, snap.timestamp)

        return canvas

    # -- Info bar (4 panels) -------------------------------------------------

    def _render_info_bar(self, snap: SensorFrame) -> np.ndarray:
        bar = np.full((BAR_H, WIN_W, 3), C_PANEL, dtype=np.uint8)

        # Panel dividers
        panel_w = WIN_W // 4
        for i in range(1, 4):
            x = i * panel_w
            cv2.line(bar, (x, 4), (x, BAR_H - 4), (80, 80, 80), 1)

        # Panel 0: Detected objects
        self._panel_objects(bar, snap, x=0, w=panel_w)

        # Panel 1: Distance
        self._panel_distance(bar, snap, x=panel_w, w=panel_w)

        # Panel 2: Fall detection
        self._panel_fall(bar, snap, x=panel_w * 2, w=panel_w)

        # Panel 3: System status + errors
        self._panel_status(bar, snap, x=panel_w * 3, w=panel_w)

        return bar

    def _panel_objects(self, bar, snap: SensorFrame, x: int, w: int) -> None:
        label = "OBJECTS"
        self._draw_panel_title(bar, label, x, w)

        if not snap.detections:
            self._text(bar, "none detected", x + 6, 40, 0.42, C_WHITE)
        else:
            # Show up to 4 objects
            for i, det in enumerate(snap.detections[:4]):
                conf_pct = f"{det.confidence:.0%}"
                line = f"{det.name[:14]:<14} {conf_pct}"
                colour = C_CYAN if det.confidence >= 0.7 else C_WHITE
                self._text(bar, line, x + 6, 38 + i * 20, 0.40, colour)

    def _panel_distance(self, bar, snap: SensorFrame, x: int, w: int) -> None:
        self._draw_panel_title(bar, "DISTANCE", x, w)

        if snap.distance_feet < 0:
            value_str = "N/A"
            colour = (120, 120, 120)
        else:
            value_str = f"{snap.distance_feet:.1f} ft"
            if snap.distance_feet < 3.0:
                colour = C_RED
            elif snap.distance_feet < 6.0:
                colour = C_YELLOW
            else:
                colour = C_GREEN

        cv2.putText(bar, value_str, (x + 6, 70), FONT_BOLD, 0.85, colour, 2, cv2.LINE_AA)

        # Mini bar graphic
        if snap.distance_feet >= 0:
            max_ft   = 12.0
            fill_pct = min(1.0, snap.distance_feet / max_ft)
            bar_x1, bar_y1 = x + 6, 88
            bar_x2, bar_y2 = x + w - 6, 100
            cv2.rectangle(bar, (bar_x1, bar_y1), (bar_x2, bar_y2), (70, 70, 70), -1)
            fill_x2 = bar_x1 + int((bar_x2 - bar_x1) * fill_pct)
            cv2.rectangle(bar, (bar_x1, bar_y1), (fill_x2, bar_y2), colour, -1)

    def _panel_fall(self, bar, snap: SensorFrame, x: int, w: int) -> None:
        self._draw_panel_title(bar, "FALL DETECT", x, w)
        status  = snap.fall.status
        colour  = FALL_COLOURS.get(status, C_WHITE)
        display = status.replace("_", " ").upper()
        self._text(bar, display, x + 6, 50, 0.45, colour)
        self._text(bar, f"Impact: {snap.fall.impact_g:.2f} g", x + 6, 72, 0.42, C_WHITE)

        # Flash border on emergency
        if status in ("impact_detected", "possible_fall"):
            alpha = int(abs(math.sin(time.time() * 4)) * 200)
            cv2.rectangle(bar, (x + 2, 2), (x + w - 2, BAR_H - 2),
                          (0, 0, alpha), 2)

    def _panel_status(self, bar, snap: SensorFrame, x: int, w: int) -> None:
        self._draw_panel_title(bar, "SYS STATUS", x, w)
        status  = snap.system_status.upper()
        colour  = STATUS_COLOURS.get(snap.system_status, C_WHITE)
        self._text(bar, status, x + 6, 50, 0.55, colour)
        self._text(bar, f"FPS: {snap.fps:.1f}", x + 6, 74, 0.42, C_CYAN)

        if snap.errors:
            err_short = snap.errors[-1][:22]
            self._text(bar, f"! {err_short}", x + 6, 96, 0.35, C_ORANGE)

    # -- Overlays ------------------------------------------------------------

    def _draw_fps_badge(self, canvas: np.ndarray, fps: float) -> None:
        label = f"FPS {fps:.1f}"
        cv2.rectangle(canvas, (0, 0), (90, 22), C_BLACK, -1)
        self._text(canvas, label, 4, 16, 0.50, C_GREEN)

    def _draw_timestamp(self, canvas: np.ndarray, ts: str) -> None:
        short = ts[11:23] if len(ts) > 23 else ts   # HH:MM:SS.mmm
        tw, _ = cv2.getTextSize(short, FONT, 0.40, 1)[0], 0
        self._text(canvas, short, WIN_W - 100, 16, 0.40, (150, 150, 150))

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _draw_panel_title(bar, label: str, x: int, w: int) -> None:
        cv2.rectangle(bar, (x, 0), (x + w, 22), (60, 60, 60), -1)
        cv2.putText(bar, label, (x + 6, 15),
                    FONT, 0.45, C_ACCENT, 1, cv2.LINE_AA)

    @staticmethod
    def _text(
        img: np.ndarray,
        text: str,
        x: int, y: int,
        scale: float,
        colour,
    ) -> None:
        cv2.putText(img, text, (x, y), FONT, scale, colour, 1, cv2.LINE_AA)

    @staticmethod
    def _make_no_signal() -> np.ndarray:
        img = np.zeros((CAM_H, CAM_W, 3), dtype=np.uint8)
        img[:] = (25, 25, 25)
        cv2.putText(img, "NO SIGNAL", (180, 230), FONT_BOLD, 1.8, (60, 60, 60), 3)
        cv2.putText(img, "Waiting for camera...", (160, 280), FONT, 0.65, (80, 80, 80), 1)
        return img


# Resolve math import needed by _panel_fall
import math

# ---------------------------------------------------------------------------
# UI thread
# ---------------------------------------------------------------------------

class UIThread(threading.Thread):
    """
    Dedicated display thread.
    Runs at ~30 FPS display rate (decoupled from inference rate).
    Must be started from the main thread on RPi (OpenCV requires it for display).
    Non-blocking: if imshow() fails (headless), gracefully degrades.
    """

    TARGET_DISPLAY_FPS = 30
    _FRAME_INTERVAL    = 1.0 / TARGET_DISPLAY_FPS

    def __init__(self, state: SharedState) -> None:
        super().__init__(name="UIThread", daemon=True)
        self._state     = state
        self._dashboard = Dashboard()
        self._headless  = False

    def run(self) -> None:
        log.info("UIThread starting")

        # Attempt window creation
        # NOTE: Fullscreen disabled — running via TigerVNC from Windows client.
        # Forcing fullscreen over VNC hijacks the remote session; windowed mode
        # is more practical for remote monitoring.
        try:
            cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(WINDOW_NAME, WIN_W, WIN_H)
        except Exception as exc:
            log.warning("OpenCV window creation failed (headless?): %s", exc)
            self._headless = True

        while not self._state.is_shutdown_requested():
            t0 = time.perf_counter()

            snap = self._state.snapshot()
            frame = self._dashboard.render(snap)

            if not self._headless:
                try:
                    cv2.imshow(WINDOW_NAME, frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord('q') or key == 27:   # Q or ESC
                        log.info("UI: quit requested by keypress")
                        self._state.request_shutdown()
                        break
                except Exception as exc:
                    log.warning("imshow error: %s — switching to headless", exc)
                    self._headless = True

            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, self._FRAME_INTERVAL - elapsed))

        if not self._headless:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

        log.info("UIThread stopped")
