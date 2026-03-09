import asyncio
import logging
import threading
import websockets

from utils import SharedState
from models import SensorPayload, DetectionModel, FallDetectionModel

log = logging.getLogger("iris.websocket")


def build_payload(state: SharedState):

    snap = state.snapshot()

    payload = SensorPayload(
        timestamp=snap.timestamp,
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


class WebSocketThread(threading.Thread):

    def __init__(self, state: SharedState, host="0.0.0.0", port=8765):
        super().__init__(name="WebSocketThread", daemon=True)

        self.state = state
        self.host = host
        self.port = port

    async def handler(self, websocket):

        log.info("Client connected")

        try:
            while not self.state.is_shutdown_requested():

                data = build_payload(self.state)

                await websocket.send(data)

                await asyncio.sleep(0.5)

        except websockets.ConnectionClosed:
            log.info("Client disconnected")

        except Exception as e:
            log.error(f"WebSocket handler error: {e}")

    async def run_server(self):

        try:
            async with websockets.serve(
                self.handler,
                self.host,
                self.port,
                reuse_port=True,
            ):

                log.info(f"WebSocket running on ws://{self.host}:{self.port}")

                while not self.state.is_shutdown_requested():
                    await asyncio.sleep(1)

        except OSError as e:
            log.error(f"WebSocket failed to start: {e}")

        except Exception as e:
            log.error(f"Unexpected WebSocket error: {e}")

    def run(self):
        asyncio.run(self.run_server())
