"""ByteTrack multi-object tracking via supervision + Ultralytics."""

from __future__ import annotations

from typing import Any

import numpy as np


def make_byte_tracker(frame_rate: float = 25.0):
    import supervision as sv

    return sv.ByteTrack(frame_rate=frame_rate)


def update_tracks(tracker, detections: list[dict[str, Any]], frame_shape: tuple[int, int]):
    """Feed person detections into ByteTrack; return supervision Detections with tracker_id."""
    import supervision as sv

    persons = [d for d in detections if d.get('role') == 'player']
    if not persons:
        empty = sv.Detections.empty()
        return tracker.update_with_detections(empty)

    xyxy = np.array([d['xyxy'] for d in persons], dtype=np.float32)
    conf = np.array([d['conf'] for d in persons], dtype=np.float32)
    class_id = np.zeros(len(persons), dtype=int)
    det = sv.Detections(xyxy=xyxy, confidence=conf, class_id=class_id)
    return tracker.update_with_detections(det)


def ball_centroid(detections: list[dict[str, Any]]) -> tuple[float, float] | None:
    balls = [d for d in detections if d.get('role') == 'ball']
    if not balls:
        return None
    best = max(balls, key=lambda d: d['conf'])
    x1, y1, x2, y2 = best['xyxy']
    return (0.5 * (x1 + x2), 0.5 * (y1 + y2))
