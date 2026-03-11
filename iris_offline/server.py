"""
server.py — WebSocket server for IRIS 2.0 Offline Module

Streams a JSON object every 0.5 s to every connected client.
The payload mirrors what is printed on the terminal status line, so your
React Native app always has live sensor + detection data to display.

JSON shape sent on every tick
──────────────────────────────
{
  "timestamp":     "2026-03-10T12:34:56.789Z",  // ISO-8601 UTC
  "fps":           14.2,
  "distance_feet": 3.45,
  "system_status": "clear",          // "clear" | "warning" | "emergency"
  "fall_detection": {
    "status":    "normal",           // "normal" | "impact_detected" | "possible_fall"
    "impact_g":  1.02
  },
  "vision": [                        // top detections from YOLO
    { "name": "person", "confidence": 0.91 },
    ...
  ],
  "errors": []                       // list of active error strings
}
"""

import asyncio
import logging
import threading

import websockets

from utils import SharedState
from models import SensorPayload, DetectionModel, FallDetectionModel

log = logging.getLogger("iris.websocket")

# How often (seconds) a JSON update is pushed to each connected client.
PUSH_INTERVAL = 0.5


def _build_payload(state: SharedState) -> str:
    """Serialise the latest sensor readings to a JSON string."""
    from datetime import datetime, timezone
    snap = state.snapshot()

    payload = SensorPayload(
        timestamp=snap.timestamp or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        vision=[
            DetectionModel(name=d.name, confidence=d.confidence)
            for d in snap.detections
        ],
        distance_feet=snap.distance_feet,
        fall_detection=FallDetectionModel(
            status=snap.fall.status,
            impact_g=snap.fall.impact_g,
        ),
        system_status=snap.system_status,
        fps=snap.fps,
        errors=snap.errors,
    )

    return payload.model_dump_json()


class ServerThread(threading.Thread):
    """
    WebSocket server that pushes a JSON sensor-data payload every
    PUSH_INTERVAL seconds to every connected React Native (or any other)
    client.

    Connect to:  ws://<pi-ip>:8765
    """

    def __init__(self, state: SharedState, host: str = "0.0.0.0", port: int = 8765):
        super().__init__(name="ServerThread", daemon=True)
        self.state = state
        self.host  = host
        self.port  = port

    async def _handler(self, websocket) -> None:
        """Push JSON sensor data to one connected client until it disconnects."""
        log.info("Client connected from %s", websocket.remote_address)
        if self.state.is_shutdown_requested():
            log.warning(
                "Handler invoked but shutdown already requested — closing %s immediately",
                websocket.remote_address,
            )
            return
        try:
            while not self.state.is_shutdown_requested():
                await websocket.send(_build_payload(self.state))
                await asyncio.sleep(PUSH_INTERVAL)
        except websockets.ConnectionClosed:
            log.info("Client disconnected from %s", websocket.remote_address)
        except Exception as exc:
            log.error("WebSocket handler error for %s: %s", websocket.remote_address, exc, exc_info=True)

    async def _run_server(self) -> None:
        try:
            async with websockets.serve(
                self._handler,
                self.host,
                self.port,
                reuse_port=True,
            ):
                log.info(
                    "WebSocket JSON stream running on ws://%s:%d",
                    self.host, self.port,
                )
                while not self.state.is_shutdown_requested():
                    await asyncio.sleep(1)

        except OSError as exc:
            log.error("WebSocket server failed to start: %s", exc)
        except Exception as exc:
            log.error("Unexpected WebSocket server error: %s", exc)

    def run(self) -> None:
        asyncio.run(self._run_server())
