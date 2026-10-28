"""Unit tests for tracking insights (no YOLO / no video)."""

from __future__ import annotations

import pandas as pd

from tracking.insights.match_metrics import team_metrics
from tracking.insights.phases import label_phase, segment_phases
from tracking.insights.player_metrics import player_metrics
from tracking.paths import PILOT_KICKOFF_OFFSET_S


def test_kickoff_offset_constant():
    assert PILOT_KICKOFF_OFFSET_S == 453.0


def test_player_distance_and_sprint():
    tracks = pd.DataFrame([
        {'t_match_s': 0.0, 'track_id': 1, 'team': 'Arsenal', 'role': 'player',
         'x_m': 0.0, 'y_m': 0.0, 'x_px': 0, 'y_px': 0, 'conf': 1,
         't_video_s': 453},
        {'t_match_s': 1.0, 'track_id': 1, 'team': 'Arsenal', 'role': 'player',
         'x_m': 8.0, 'y_m': 0.0, 'x_px': 0, 'y_px': 0, 'conf': 1,
         't_video_s': 454},
    ])
    rows = player_metrics(tracks)
    assert len(rows) == 1
    assert rows[0]['distance_m'] == 8.0
    assert rows[0]['sprint_count'] >= 1


def test_team_metrics_nonempty():
    rows = []
    for t in range(10):
        for i, x in enumerate([20.0, 22.0, 24.0, 26.0]):
            rows.append({
                't_match_s': float(t), 'track_id': i, 'team': 'Arsenal',
                'role': 'player', 'x_m': x, 'y_m': 10.0 + i,
                'x_px': 0, 'y_px': 0, 'conf': 1, 't_video_s': 453 + t,
            })
    metrics = team_metrics(pd.DataFrame(rows))
    assert metrics and metrics[0]['team'] == 'Arsenal'
    assert metrics[0]['avg_line_height_m'] is not None


def test_phase_labels():
    assert label_phase(20.0, 30.0) == 'low_block'
    assert label_phase(60.0, 70.0) == 'high_press'


def test_segment_phases_merges():
    rows = []
    for t in range(0, 20):
        for i in range(4):
            rows.append({
                't_match_s': float(t), 'track_id': i, 'team': 'Arsenal',
                'role': 'player', 'x_m': 25.0 + i, 'y_m': 20.0 + i,
                'x_px': 0, 'y_px': 0, 'conf': 1, 't_video_s': 453 + t,
            })
    phases = segment_phases(pd.DataFrame(rows))
    assert phases
    assert phases[0]['phase'] in {
        'low_block', 'mid_block', 'high_press', 'settled_attack'}
