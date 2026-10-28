"""Player-level tracking metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tracking.paths import PITCH_LENGTH_M

SPRINT_MPS = 7.0


def player_metrics(tracks: pd.DataFrame) -> list[dict]:
    if tracks.empty:
        return []
    players = tracks[tracks['role'] == 'player'].copy()
    if players.empty:
        return []
    rows = []
    for track_id, g in players.groupby('track_id'):
        g = g.sort_values('t_match_s')
        xs = g['x_m'].to_numpy(dtype=float)
        ys = g['y_m'].to_numpy(dtype=float)
        ts = g['t_match_s'].to_numpy(dtype=float)
        dist = 0.0
        sprints = 0
        for i in range(1, len(ts)):
            dt = max(ts[i] - ts[i - 1], 1e-6)
            step = float(np.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]))
            dist += step
            if step / dt >= SPRINT_MPS:
                sprints += 1
        final_share = float(np.mean(xs > (2.0 / 3.0) * PITCH_LENGTH_M)) if len(xs) else 0.0
        team = g['team'].dropna().mode()
        rows.append({
            'track_id': int(track_id),
            'team': None if team.empty else str(team.iloc[0]),
            'role': 'player',
            'distance_m': round(dist, 2),
            'sprint_count': int(sprints),
            'avg_x_m': round(float(np.nanmean(xs)), 3),
            'avg_y_m': round(float(np.nanmean(ys)), 3),
            'final_third_share': round(final_share, 4),
            'frames': int(len(g)),
        })
    return rows
