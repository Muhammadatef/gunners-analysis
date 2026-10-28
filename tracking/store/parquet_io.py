"""Parquet I/O for tracks and phases."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tracking.paths import phases_path, tracks_path


TRACK_COLUMNS = [
    't_video_s', 't_match_s', 'track_id', 'team', 'role',
    'x_px', 'y_px', 'x_m', 'y_m', 'conf',
]


def write_tracks(match_id: int, rows: list[dict], path: Path | None = None) -> Path:
    out = path or tracks_path(match_id)
    df = pd.DataFrame(rows)
    for col in TRACK_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[TRACK_COLUMNS]
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return out


def read_tracks(match_id: int) -> pd.DataFrame:
    path = tracks_path(match_id)
    if not path.is_file():
        return pd.DataFrame(columns=TRACK_COLUMNS)
    return pd.read_parquet(path)


def write_phases(match_id: int, rows: list[dict]) -> Path:
    out = phases_path(match_id)
    df = pd.DataFrame(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return out


def read_phases(match_id: int) -> pd.DataFrame:
    path = phases_path(match_id)
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_parquet(path)
