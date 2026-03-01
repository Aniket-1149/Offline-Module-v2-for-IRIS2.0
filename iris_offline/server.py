"""
server.py — HTTPS JSON streaming server (Flask)
IRIS 2.0 Offline Module

Endpoints:
  GET  https://<ip>:5000/vision       → Latest sensor snapshot as JSON
  GET  https://<ip>:5000/health       → Server health ping
  GET  https://<ip>:5000/stream       → MJPEG live video stream

TLS: self-signed cert/key must be generated before first run.
See setup.sh for the openssl command.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Generator

from flask import Flask, Response, jsonify

from utils import SharedState, SensorFrame

log = logging.getLogger("iris.server")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
CERT_FILE = BASE_DIR / "cert.pem"
KEY_FILE  = BASE_DIR / "key.pem"

# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def _build_app(state: SharedState) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # ------------------------------------------------------------------
    # /vision — primary JSON endpoint
    # ------------------------------------------------------------------
    @app.route("/vision", methods=["GET"])
    def vision_endpoint():
        try:
            snap = state.snapshot()
            payload = _serialise(snap)
            return Response(
                json.dumps(payload, ensure_ascii=False),
                status=200,
                mimetype="application/json",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except Exception as exc:
            log.error("/vision error: %s", exc)
            return Response(
                json.dumps({
                    "timestamp": _iso_now(),
                    "error": str(exc),
                    "system_status": "error",
                }),
                status=500,
                mimetype="application/json",
            )

    # ------------------------------------------------------------------
    # /health — lightweight liveness probe
    # ------------------------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health_endpoint():
        snap = state.snapshot()
        return jsonify({
            "status": "ok",
            "system_status": snap.system_status,
            "fps": snap.fps,
            "uptime_s": round(time.time() - _start_time, 1),
        })

    # ------------------------------------------------------------------
    # /stream — MJPEG live video for browser preview
    # ------------------------------------------------------------------
    @app.route("/stream", methods=["GET"])
    def stream_endpoint():
        return Response(
            _mjpeg_generator(state),
            mimetype="multipart/x-mixed-replace; boundary=--frame",
        )

    return app


_start_time = time.time()


def _mjpeg_generator(state: SharedState) -> Generator[bytes, None, None]:
    """Yield JPEG frames as multipart/x-mixed-replace stream."""
    import cv2
    while not state.is_shutdown_requested():
        frame = state.get_annotated_frame()
        if frame is None:
            time.sleep(0.1)
            continue
        try:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + buf.tobytes()
                + b"\r\n"
            )
        except Exception as exc:
            log.debug("MJPEG encode error: %s", exc)
        time.sleep(0.05)   # ~20 FPS stream cap


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialise(snap: SensorFrame) -> dict:
    return {
        "timestamp":       snap.timestamp,
        "vision":          [
            {"name": d.name, "confidence": d.confidence}
            for d in snap.detections
        ],
        "distance_feet":   snap.distance_feet,
        "fall_detection":  {
            "status":   snap.fall.status,
            "impact_g": snap.fall.impact_g,
        },
        "system_status":   snap.system_status,
        "fps":             snap.fps,
        "errors":          snap.errors,
    }


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# HTTPS server thread
# ---------------------------------------------------------------------------

class ServerThread(threading.Thread):
    """
    Runs Flask HTTPS server in a daemon thread.
    Uses Werkzeug's built-in SSL context for simplicity.
    For production hardening, replace with gunicorn + TLS termination.
    """

    def __init__(
        self,
        state: SharedState,
        host: str = "0.0.0.0",
        port: int = 5000,
    ) -> None:
        super().__init__(name="ServerThread", daemon=True)
        self._state = state
        self._host  = host
        self._port  = port
        self._app   = _build_app(state)

    def run(self) -> None:
        log.info("ServerThread starting on %s:%d", self._host, self._port)
        ssl_context = self._get_ssl_context()

        try:
            from werkzeug.serving import make_server as _make_server, BaseWSGIServer
            # SO_REUSEADDR must be set on the class BEFORE make_server() binds
            # the socket — setting it afterwards is too late and has no effect.
            BaseWSGIServer.allow_reuse_address = True
            srv = _make_server(
                self._host,
                self._port,
                self._app,
                ssl_context=ssl_context,
                threaded=True,
            )
            log.info("Flask HTTPS server listening on %s:%d", self._host, self._port)
            srv.serve_forever()
        except OSError as exc:
            if "Address already in use" in str(exc):
                log.critical(
                    "Port %d still in use. Run:  sudo fuser -k 5000/tcp  then restart.",
                    self._port,
                )
            else:
                log.critical("Flask server crashed: %s", exc)
            self._state.add_error(f"Server crash: {exc}")
        except Exception as exc:
            log.critical("Flask server crashed: %s", exc)
            self._state.add_error(f"Server crash: {exc}")

    def _get_ssl_context(self):
        if CERT_FILE.exists() and KEY_FILE.exists():
            log.info("TLS: Using cert=%s, key=%s", CERT_FILE, KEY_FILE)
            return (str(CERT_FILE), str(KEY_FILE))
        else:
            log.warning(
                "TLS cert/key not found — using adhoc self-signed cert. "
                "Run setup.sh to generate persistent certs."
            )
            return "adhoc"   # Werkzeug generates a temporary cert
