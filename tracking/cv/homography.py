"""Pixel ↔ pitch homography (105m × 68m, same as event warehouse)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from tracking.paths import PITCH_LENGTH_M, PITCH_WIDTH_M, homography_path


# Default broadcast-ish quad in normalised [0,1] image coords → full pitch.
# Tuned later per match via calibrate_and_save; works as a first approximation.
_DEFAULT_SRC_NORM = np.array([
    [0.12, 0.22],
    [0.88, 0.22],
    [0.98, 0.92],
    [0.02, 0.92],
], dtype=np.float32)

_DST = np.array([
    [0.0, 0.0],
    [PITCH_LENGTH_M, 0.0],
    [PITCH_LENGTH_M, PITCH_WIDTH_M],
    [0.0, PITCH_WIDTH_M],
], dtype=np.float32)


def default_matrix(frame_width: int, frame_height: int) -> np.ndarray:
    src = _DEFAULT_SRC_NORM.copy()
    src[:, 0] *= frame_width
    src[:, 1] *= frame_height
    return cv2.getPerspectiveTransform(src, _DST)


def load_or_default(match_id: int, frame_width: int, frame_height: int) -> np.ndarray:
    path = homography_path(match_id)
    if path.is_file():
        data = json.loads(path.read_text(encoding='utf-8'))
        return np.array(data['matrix'], dtype=np.float32)
    M = default_matrix(frame_width, frame_height)
    save_homography(match_id, M, note='auto-default broadcast quad')
    return M


def save_homography(match_id: int, matrix: np.ndarray, note: str = '') -> Path:
    path = homography_path(match_id)
    payload = {
        'matrix': matrix.tolist(),
        'pitch_length_m': PITCH_LENGTH_M,
        'pitch_width_m': PITCH_WIDTH_M,
        'note': note,
    }
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return path


def pixel_to_pitch(M: np.ndarray, x: float, y: float) -> tuple[float, float]:
    pts = np.array([[[x, y]]], dtype=np.float32)
    out = cv2.perspectiveTransform(pts, M)[0, 0]
    return float(out[0]), float(out[1])


def feet_point(xyxy: list[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = xyxy
    return (0.5 * (x1 + x2), y2)
