"""Filesystem layout for tracking artifacts."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get(
    'GUNNERS_ROOT',
    Path(__file__).resolve().parents[1]))

TRACKING_ROOT = ROOT / 'data' / 'tracking'
PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0

# GW35 Arsenal v Fulham pilot defaults
PILOT_MATCH_ID = 1903405
PILOT_KICKOFF_OFFSET_S = 453.0  # video 7:33
# A local recording you have the right to analyse; nothing is shipped.
PILOT_VIDEO = os.environ.get('GUNNERS_PILOT_VIDEO', '')


def match_dir(match_id: int) -> Path:
    d = TRACKING_ROOT / str(match_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def meta_path(match_id: int) -> Path:
    return match_dir(match_id) / 'meta.json'


def tracks_path(match_id: int) -> Path:
    return match_dir(match_id) / 'tracks.parquet'


def phases_path(match_id: int) -> Path:
    return match_dir(match_id) / 'phases.parquet'


def proxy_path(match_id: int) -> Path:
    return match_dir(match_id) / 'proxy.mp4'


def homography_path(match_id: int) -> Path:
    return match_dir(match_id) / 'homography.json'


def output_report_path(match_id: int = PILOT_MATCH_ID) -> Path:
    """Prefer writing next to existing GW35 panels when present."""
    season = ROOT / 'output' / '2025-2026'
    if match_id == PILOT_MATCH_ID:
        candidate = season / 'mw35_2026-05-02_arsenal_v_fulham' / 'TRACKING_REPORT.md'
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    out = match_dir(match_id) / 'TRACKING_REPORT.md'
    return out
