"""Match / team shape metrics from pitch tracks."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.spatial import ConvexHull

from tracking.paths import PITCH_LENGTH_M, PITCH_WIDTH_M


def _compactness(xs: np.ndarray, ys: np.ndarray) -> float:
    if len(xs) < 3:
        return 0.0
    try:
        pts = np.column_stack([xs, ys])
        hull = ConvexHull(pts)
        return float(hull.volume)  # 2D area
    except Exception:  # noqa: BLE001
        return float(np.ptp(xs) * np.ptp(ys))


def team_metrics(tracks: pd.DataFrame) -> list[dict]:
    if tracks.empty:
        return []
    players = tracks[tracks['role'] == 'player'].dropna(subset=['team'])
    rows = []
    for team, g in players.groupby('team'):
        # Per-frame team shape then average
        line_heights = []
        widths = []
        comps = []
        thirds = {'def': 0, 'mid': 0, 'att': 0}
        n_frames = 0
        for _, frame in g.groupby('t_match_s'):
            xs = frame['x_m'].to_numpy(dtype=float)
            ys = frame['y_m'].to_numpy(dtype=float)
            if len(xs) < 2:
                continue
            # Defensive line ≈ lowest 4 x (or highest — orientation unknown).
            # Use mean of the 4 smallest x as "line height from own goal" proxy.
            ordered = np.sort(xs)
            line = float(np.mean(ordered[: min(4, len(ordered))]))
            line_heights.append(line)
            widths.append(float(np.ptp(ys)))
            comps.append(_compactness(xs, ys))
            cx = float(np.mean(xs))
            if cx < PITCH_LENGTH_M / 3:
                thirds['def'] += 1
            elif cx < 2 * PITCH_LENGTH_M / 3:
                thirds['mid'] += 1
            else:
                thirds['att'] += 1
            n_frames += 1
        total = max(n_frames, 1)
        rows.append({
            'team': str(team),
            'avg_line_height_m': round(float(np.mean(line_heights)), 3) if line_heights else None,
            'avg_width_m': round(float(np.mean(widths)), 3) if widths else None,
            'avg_compactness': round(float(np.mean(comps)), 3) if comps else None,
            'share_def_third': round(thirds['def'] / total, 4),
            'share_mid_third': round(thirds['mid'] / total, 4),
            'share_att_third': round(thirds['att'] / total, 4),
        })
    return rows
