"""
vision.py — handles everything camera-related: grabbing frames from the ArduCam
and running them through YOLOv8n to figure out what's in the scene.
"""

import logging
import threading
import time
from pathlib import Path
from typing import List, Tuple, Optional

import cv2
import numpy as np

log = logging.getLogger("iris.vision")

# try to import ultralytics — if it's missing we just won't get any detections,
# but the rest of the system (distance, fall, server) will still run fine
try:
    from ultralytics import YOLO
    _YOLO_AVAILABLE = True
except ImportError:
    _YOLO_AVAILABLE = False
    log.warning("ultralytics not installed — vision will produce no detections")

from utils import SharedState, Detection, FPSCounter

# tweak these if you want a different resolution or strictness on detections
FRAME_WIDTH  = 640
FRAME_HEIGHT = 480
CONFIDENCE_THRESHOLD = 0.45
MODEL_PATH = Path(__file__).parent / "yolov8n.pt"   # gets downloaded automatically on first run

# all 80 object categories that YOLOv8n knows about (standard COCO dataset)
COCO_NAMES = [
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
    "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat",
    "dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack",
    "umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball",
    "kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket",
    "bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
    "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair",
    "couch","potted plant","bed","dining table","toilet","tv","laptop","mouse",
    "remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator",
    "book","clock","vase","scissors","teddy bear","hair drier","toothbrush",
]

# give each class its own colour for bounding boxes — seeded so they're always the same
_PALETTE = [
    tuple(int(x) for x in np.random.default_rng(seed=i).integers(80, 255, size=3))
    for i in range(len(COCO_NAMES))
]


# ---------------------------------------------------------------------------
# Camera wrapper with reconnect logic
# ---------------------------------------------------------------------------

class CameraSource:
    """
    Thin wrapper around cv2.VideoCapture that knows how to reopen the camera
    if it disconnects. Works with the CSI ArduCam via GStreamer, and falls
    back to plain V4L2 if GStreamer isn't available.
    """

    # GStreamer gives us the best quality and lowest latency on the RPi5 CSI camera.
    # If this doesn't work on your setup, set use_gstreamer=False in __init__.
    _GSTREAMER_PIPELINE = (
        "libcamerasrc ! "
        "video/x-raw,width=640,height=480,framerate=30/1 ! "
        "videoconvert ! appsink max-buffers=1 drop=true"
    )

    def __init__(self, index: int = 0, use_gstreamer: bool = True) -> None:
        self._index = index
        self._use_gstreamer = use_gstreamer
        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = threading.Lock()

    def open(self) -> bool:
        with self._lock:
            self._release_internal()
            self._cap = self._try_open()
            if self._cap and self._cap.isOpened():
                log.info("Camera opened successfully")
                return True
            log.error("Failed to open camera")
            return False

    def _try_open(self) -> Optional[cv2.VideoCapture]:
        # first shot: GStreamer pipeline — best option for the ArduCam on RPi5
        if self._use_gstreamer:
            cap = cv2.VideoCapture(self._GSTREAMER_PIPELINE, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                log.info("Camera: GStreamer pipeline active")
                return cap
            log.warning("GStreamer pipeline failed, falling back to V4L2 index %d", self._index)

        # GStreamer didn't work — try reading directly from the V4L2 device node
        cap = cv2.VideoCapture(self._index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self._index)   # last resort, let OpenCV figure it out
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # keep the buffer tiny so frames stay fresh
            log.info("Camera: V4L2 device %d active", self._index)
        return cap

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        with self._lock:
            if self._cap is None or not self._cap.isOpened():
                return False, None
            ret, frame = self._cap.read()
            return ret, frame

    def release(self) -> None:
        with self._lock:
            self._release_internal()

    def _release_internal(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# ---------------------------------------------------------------------------
# YOLO wrapper
# ---------------------------------------------------------------------------

class Detector:
    """Loads YOLOv8n and runs inference. Tuned to be as fast as possible on the Pi 5 CPU."""

    def __init__(self) -> None:
        self._model = None
        self._ready = False

    def load(self) -> bool:
        if not _YOLO_AVAILABLE:
            return False
        try:
            log.info("Loading YOLOv8n model from %s ...", MODEL_PATH)
            self._model = YOLO(str(MODEL_PATH))
            # run a blank frame through the model once so the first real frame isn't slow
            dummy = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
            self._model.predict(
                dummy,
                imgsz=FRAME_WIDTH,
                conf=CONFIDENCE_THRESHOLD,
                verbose=False,
                device="cpu",
            )
            self._ready = True
            log.info("YOLOv8n ready")
            return True
        except Exception as exc:
            log.error("YOLOv8n load failed: %s", exc)
            return False

    def infer(self, frame: np.ndarray) -> Tuple[List[Detection], np.ndarray]:
        """
        Pass a BGR frame through YOLOv8n.
        Returns the list of detections and a copy of the frame with boxes drawn on it.
        """
        if not self._ready or self._model is None:
            return [], frame.copy()

        results = self._model.predict(
            frame,
            imgsz=FRAME_WIDTH,
            conf=CONFIDENCE_THRESHOLD,
            verbose=False,
            device="cpu",
            half=False,     # FP16 only works on GPU, not here
            max_det=20,     # 20 objects is plenty — caps CPU time nicely
        )

        detections: List[Detection] = []
        annotated = frame.copy()

        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                cls_id   = int(box.cls[0].item())
                conf     = float(box.conf[0].item())
                name     = COCO_NAMES[cls_id] if cls_id < len(COCO_NAMES) else str(cls_id)
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                colour   = _PALETTE[cls_id % len(_PALETTE)]

                detections.append(Detection(
                    name=name,
                    confidence=round(conf, 3),
                    bbox=[x1, y1, x2, y2],
                ))

                # draw the box and a filled label bar above it
                cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 2)
                label = f"{name} {conf:.0%}"
                (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
                cv2.rectangle(annotated, (x1, y1 - th - baseline - 4), (x1 + tw + 4, y1), colour, -1)
                cv2.putText(
                    annotated, label,
                    (x1 + 2, y1 - baseline - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA,
                )

        return detections, annotated


# ---------------------------------------------------------------------------
# Vision thread
# ---------------------------------------------------------------------------

class VisionThread(threading.Thread):
    """
    Runs in the background, continuously pulling frames from the camera
    and feeding them through YOLO. Results get pushed into SharedState
    so the server and UI always have something fresh to show.
    """

    _RECONNECT_DELAY = 3.0   # how long to wait before trying to reopen the camera
    _SKIP_FRAMES     = 1     # inference every other frame — halves CPU load with barely any accuracy loss

    def __init__(self, state: SharedState) -> None:
        super().__init__(name="VisionThread", daemon=True)
        self._state    = state
        self._camera   = CameraSource()
        self._detector = Detector()
        self._fps      = FPSCounter(window=20)

    def run(self) -> None:
        log.info("VisionThread starting")
        if not self._detector.load():
            self._state.add_error("YOLOv8 model failed to load")

        if not self._camera.open():
            self._state.add_error("Camera failed to open at startup")

        skip_counter = 0

        while not self._state.is_shutdown_requested():
            ok, frame = self._camera.read()

            if not ok or frame is None:
                log.warning("Camera read failed — attempting reconnect in %.1fs", self._RECONNECT_DELAY)
                self._state.add_error("Camera read error")
                self._camera.release()
                time.sleep(self._RECONNECT_DELAY)
                self._camera.open()
                continue

            fps = self._fps.tick()

            # only run YOLO on every other frame to keep CPU usage manageable
            skip_counter = (skip_counter + 1) % (self._SKIP_FRAMES + 1)
            if skip_counter == 0:
                try:
                    detections, annotated = self._detector.infer(frame)
                except Exception as exc:
                    log.error("Inference error: %s", exc)
                    detections, annotated = [], frame.copy()
                    self._state.add_error(f"Inference error: {exc}")
            else:
                # skipped frame — carry over the previous detections, just update the image
                snap = self._state.snapshot()
                detections  = snap.detections
                annotated   = frame.copy()

            self._state.update_vision(detections, fps, frame, annotated)

        self._camera.release()
        log.info("VisionThread stopped")
