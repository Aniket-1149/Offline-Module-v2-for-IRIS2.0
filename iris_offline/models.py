from pydantic import BaseModel
from typing import List


class DetectionModel(BaseModel):
    name: str
    confidence: float


class FallDetectionModel(BaseModel):
    status: str
    impact_g: float


class SensorPayload(BaseModel):
    timestamp: str
    vision: List[DetectionModel]
    distance_feet: float
    fall_detection: FallDetectionModel
    system_status: str
    fps: float
    errors: List[str]
