"""YOLO detection helpers (Ultralytics)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# COCO class ids
CLS_PERSON = 0
CLS_SPORTS_BALL = 32


def load_model(weights: str = 'yolov8n.pt', device: str | None = None):
    from ultralytics import YOLO

    model = YOLO(weights)
    # Prefer CUDA when available; callers may force cpu for MX350 OOM recovery.
    if device:
        try:
            model.to(device)
        except Exception as exc:  # noqa: BLE001
            logger.warning('could not move model to %s: %s', device, exc)
    return model


def detect_frame(model, frame, *, imgsz: int = 640, conf: float = 0.25) -> list[dict[str, Any]]:
    """Return list of {cls, conf, xyxy} for person + sports ball."""
    results = model.predict(frame, imgsz=imgsz, conf=conf, verbose=False, classes=[CLS_PERSON, CLS_SPORTS_BALL])
    out: list[dict[str, Any]] = []
    if not results:
        return out
    r0 = results[0]
    if r0.boxes is None:
        return out
    boxes = r0.boxes
    for i in range(len(boxes)):
        cls_id = int(boxes.cls[i].item())
        conf_v = float(boxes.conf[i].item())
        xyxy = boxes.xyxy[i].tolist()
        role = 'ball' if cls_id == CLS_SPORTS_BALL else 'player'
        out.append({
            'cls': cls_id,
            'role': role,
            'conf': conf_v,
            'xyxy': [float(x) for x in xyxy],
        })
    return out
