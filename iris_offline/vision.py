"""
vision.py — handles everything camera-related: grabbing frames from the Pi camera
using picamera2 and running them through YOLOv8n to figure out what's in the scene.
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

# picamera2 is the official Pi camera library — installed as a system package
# on Raspberry Pi OS. It sits on top of libcamera and is the right way to talk
# to any Pi CSI camera (Camera Module v1/v2/v3, ArduCam, HQ Camera, etc.)
try:
    from picamera2 import Picamera2
    _PICAM2_AVAILABLE = True
except ImportError:
    _PICAM2_AVAILABLE = False
    log.warning("picamera2 not available — camera frames will be unavailable")

from utils import SharedState, Detection, FPSCounter

# tweak these if you want a different resolution or strictness on detections
FRAME_WIDTH  = 640
FRAME_HEIGHT = 480
CONFIDENCE_THRESHOLD = 0.45
MODEL_PATH = Path(__file__).parent / "yolov8n.pt"   # gets downloaded automatically on first run
ONNX_PATH  = MODEL_PATH.with_suffix(".onnx")         # exported on first run for faster CPU inference

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
    Grabs frames from the Pi camera using picamera2.
    picamera2 is libcamera-based, so it works with every CSI camera on the Pi
    without needing GStreamer or V4L2 tricks.
    Returns BGR frames so the rest of the pipeline (OpenCV, YOLO) gets what it expects.
    """

    def __init__(self) -> None:
        self._cam: Optional["Picamera2"] = None
        self._lock = threading.Lock()
        self._started = False

    def open(self) -> bool:
        with self._lock:
            self._close_internal()
            if not _PICAM2_AVAILABLE:
                log.warning("picamera2 not installed — running without camera")
                return False
            try:
                self._cam = Picamera2()
                config = self._cam.create_preview_configuration(
                    main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"},
                    buffer_count=2,   # two buffers is enough — keeps memory low and frames fresh
                )
                self._cam.configure(config)
                self._cam.start()
                self._started = True
                log.info("PiCamera2 started at %dx%d", FRAME_WIDTH, FRAME_HEIGHT)
                return True
            except Exception as exc:
                log.error("PiCamera2 open failed: %s", exc)
                self._close_internal()
                return False

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        with self._lock:
            if not self._started or self._cam is None:
                return False, None
            try:
                # picamera2 gives us RGB — flip to BGR because that's what OpenCV and YOLO want
                rgb = self._cam.capture_array()
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                return True, bgr
            except Exception as exc:
                log.warning("PiCamera2 read error: %s", exc)
                return False, None

    def release(self) -> None:
        with self._lock:
            self._close_internal()

    def _close_internal(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass
            self._cam = None
        self._started = False


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
            # ── one-time ONNX export ──────────────────────────────────────────
            # ONNX inference via onnxruntime is 2-3× faster than the PyTorch .pt
            # on CPU (Pi 5 ARM64). We export once and reuse the .onnx on every
            # subsequent launch — the export takes ~30 seconds but only happens
            # the first time main.py is run after setup.
            if not ONNX_PATH.exists():
                try:
                    log.info("Exporting YOLOv8n to ONNX for faster CPU inference "
                             "(one-time ~30s) ...")
                    _export_model = YOLO(str(MODEL_PATH))
                    _export_model.export(
                        format="onnx",
                        imgsz=FRAME_WIDTH,
                        half=False,
                        dynamic=False,
                        simplify=True,
                        opset=12,
                    )
                    if ONNX_PATH.exists():
                        log.info("ONNX export complete → %s", ONNX_PATH)
                    else:
                        log.warning("ONNX export did not produce expected file; "
                                    "will use .pt this run")
                except Exception as export_exc:
                    log.warning(
                        "ONNX export failed (%s) — running with .pt (slower). "
                        "Run: pip install onnx onnxslim onnxruntime  in the venv to fix.",
                        export_exc,
                    )

            load_path = str(ONNX_PATH) if ONNX_PATH.exists() else str(MODEL_PATH)
            log.info("Loading model: %s", load_path)
            self._model = YOLO(load_path)

            # warm-up pass so the first real frame isn't slow
            dummy = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
            self._model.predict(
                dummy,
                imgsz=FRAME_WIDTH,
                conf=CONFIDENCE_THRESHOLD,
                verbose=False,
                device="cpu",
            )
            self._ready = True
            log.info("YOLOv8n ready (ONNX=%s)", ONNX_PATH.exists())
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
    _SKIP_FRAMES     = 0     # run YOLO on every frame — ONNX is fast enough on Pi 5

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
                if self._camera.open():
                    # Camera recovered — remove stale camera errors from the list
                    self._state.clear_error_prefix("Camera")
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
