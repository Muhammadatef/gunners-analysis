"""K-Means jersey colour team assignment (Arsenal red vs opponent)."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from sklearn.cluster import KMeans


def _jersey_crop(frame: np.ndarray, xyxy: list[float]) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in xyxy]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    # Upper torso band
    yh = y1 + int(0.45 * (y2 - y1))
    crop = frame[y1:yh, x1:x2]
    if crop.size == 0:
        return None
    return crop


def _mean_hsv(crop: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # Drop near-green pitch contamination (high S green-ish)
    pixels = hsv.reshape(-1, 3).astype(np.float32)
    return pixels.mean(axis=0)


def assign_teams(
    frame: np.ndarray,
    tracks: list[dict[str, Any]],
    *,
    home_name: str = 'Arsenal',
    away_name: str = 'Fulham',
) -> list[dict[str, Any]]:
    """Mutate/return tracks with `team` set for player roles.

    Heuristic: cluster jersey mean hue into 2 groups; the redder cluster is Arsenal.
    """
    players = [t for t in tracks if t.get('role') == 'player']
    features: list[np.ndarray] = []
    idxs: list[int] = []
    for i, t in enumerate(players):
        crop = _jersey_crop(frame, t['xyxy'])
        if crop is None:
            continue
        features.append(_mean_hsv(crop))
        idxs.append(i)
    if len(features) < 4:
        for t in tracks:
            if t.get('role') == 'player':
                t.setdefault('team', None)
        return tracks

    X = np.vstack(features)
    km = KMeans(n_clusters=2, n_init=10, random_state=0)
    labels = km.fit_predict(X)
    centres = km.cluster_centers_
    # Higher hue near red wraps; use mean of R-ish: low hue or high hue + high sat
    # Prefer cluster with higher saturation and hue near 0/180 as Arsenal red.
    scores = []
    for c in centres:
        h, s, v = c
        redish = min(abs(h - 0), abs(h - 180))
        scores.append(s - 0.3 * redish + 0.01 * v)
    arsenal_label = int(np.argmax(scores))

    label_by_player_idx = {idxs[j]: int(labels[j]) for j in range(len(idxs))}
    for i, t in enumerate(players):
        lab = label_by_player_idx.get(i)
        if lab is None:
            t['team'] = None
        elif lab == arsenal_label:
            t['team'] = home_name
        else:
            t['team'] = away_name
    for t in tracks:
        if t.get('role') == 'ball':
            t['team'] = None
    return tracks
