"""Rule-based phase segmentation from tracking (+ optional event seeds)."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from tracking.paths import PITCH_LENGTH_M

logger = logging.getLogger(__name__)

# Line-height thresholds in metres from "own" low-x goal (v1 orientation).
LOW_BLOCK_MAX = 35.0
MID_BLOCK_MAX = 55.0
HIGH_PRESS_MIN = 55.0
MIN_PHASE_S = 4.0


def _frame_team_state(frame: pd.DataFrame) -> dict[str, float]:
    xs = frame['x_m'].to_numpy(dtype=float)
    ys = frame['y_m'].to_numpy(dtype=float)
    ordered = np.sort(xs)
    line = float(np.mean(ordered[: min(4, len(ordered))])) if len(ordered) else 0.0
    width = float(np.ptp(ys)) if len(ys) else 0.0
    cx = float(np.mean(xs)) if len(xs) else 0.0
    return {'line': line, 'width': width, 'cx': cx}


def label_phase(line: float, cx: float) -> str:
    if line <= LOW_BLOCK_MAX and cx < PITCH_LENGTH_M / 2:
        return 'low_block'
    if line <= MID_BLOCK_MAX:
        return 'mid_block'
    if line >= HIGH_PRESS_MIN:
        return 'high_press'
    if cx > 2 * PITCH_LENGTH_M / 3:
        return 'settled_attack'
    return 'mid_block'


def segment_phases(tracks: pd.DataFrame) -> list[dict]:
    if tracks.empty:
        return []
    players = tracks[tracks['role'] == 'player'].dropna(subset=['team'])
    phases: list[dict] = []
    for team, g in players.groupby('team'):
        timeline = []
        for t, frame in g.groupby('t_match_s'):
            st = _frame_team_state(frame)
            timeline.append((float(t), label_phase(st['line'], st['cx']), st))
        if not timeline:
            continue
        # Merge consecutive labels
        cur_label = timeline[0][1]
        start = timeline[0][0]
        feats: dict[str, Any] = {'samples': 1, 'line_sum': timeline[0][2]['line']}
        for t, lab, st in timeline[1:]:
            if lab == cur_label:
                feats['samples'] += 1
                feats['line_sum'] += st['line']
            else:
                end = t
                if end - start >= MIN_PHASE_S:
                    phases.append({
                        'team': str(team),
                        'phase': cur_label,
                        't_start_s': round(start, 3),
                        't_end_s': round(end, 3),
                        'features': {
                            'avg_line_height_m': round(
                                feats['line_sum'] / max(feats['samples'], 1), 3),
                            'samples': feats['samples'],
                        },
                    })
                cur_label = lab
                start = t
                feats = {'samples': 1, 'line_sum': st['line']}
        end = timeline[-1][0]
        if end - start >= MIN_PHASE_S:
            phases.append({
                'team': str(team),
                'phase': cur_label,
                't_start_s': round(start, 3),
                't_end_s': round(end, 3),
                'features': {
                    'avg_line_height_m': round(
                        feats['line_sum'] / max(feats['samples'], 1), 3),
                    'samples': feats['samples'],
                },
            })
        phases.extend(_transition_windows(timeline, str(team)))
    return phases


def _transition_windows(
    timeline: list[tuple[float, str, dict[str, float]]],
    team: str,
    *,
    window_s: float = 8.0,
    line_delta_m: float = 15.0,
    cooldown_s: float = 10.0,
) -> list[dict]:
    """Flag large defensive-line jumps within ~8s as transition phases."""
    if len(timeline) < 2:
        return []
    out: list[dict] = []
    next_ok = timeline[0][0]
    i = 0
    while i < len(timeline):
        t0, _, st0 = timeline[i]
        if t0 < next_ok:
            i += 1
            continue
        j = i + 1
        best_delta = 0.0
        t1 = t0
        while j < len(timeline) and timeline[j][0] - t0 <= window_s:
            delta = abs(timeline[j][2]['line'] - st0['line'])
            if delta > best_delta:
                best_delta = delta
                t1 = timeline[j][0]
            j += 1
        if best_delta >= line_delta_m and t1 - t0 >= 2.0:
            out.append({
                'team': team,
                'phase': 'transition',
                't_start_s': round(t0, 3),
                't_end_s': round(t1, 3),
                'features': {
                    'line_delta_m': round(best_delta, 3),
                    'window_s': window_s,
                },
            })
            next_ok = t1 + cooldown_s
        i += 1
    return out


def seed_set_pieces_from_events(match_id: int, offset_s: float = 0.0) -> list[dict]:
    """Best-effort: pull corner/FK minutes from events if warehouse available."""
    try:
        from db.connection import connect
    except Exception:  # noqa: BLE001
        return []
    rows = []
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                # Opta-style: top-level `type` + jsonb `qualifiers` keys.
                cur.execute(
                    """
                    SELECT minute, second, type, team_id, qualifiers
                    FROM events
                    WHERE match_id = %s
                      AND (
                        type ILIKE '%%Corner%%'
                        OR type ILIKE '%%FreeKick%%'
                        OR type ILIKE '%%Free Kick%%'
                        OR qualifiers ?| ARRAY[
                          'CornerTaken', 'FromCorner', 'FreekickTaken',
                          'IndirectFreekickTaken', 'DirectFreekick', 'SetPiece'
                        ]
                      )
                    ORDER BY minute, second
                    LIMIT 80
                    """,
                    (match_id,),
                )
                for minute, second, type_name, team_id, quals in cur.fetchall():
                    t = float(minute or 0) * 60.0 + float(second or 0)
                    rows.append({
                        'team': None,
                        'phase': 'set_piece',
                        't_start_s': round(t, 3),
                        't_end_s': round(t + 15.0, 3),
                        'features': {
                            'source': 'events',
                            'type': type_name,
                            'team_id': team_id,
                            'qualifiers': quals if isinstance(quals, dict) else None,
                            'offset_applied': offset_s,
                        },
                    })
    except Exception as exc:  # noqa: BLE001
        logger.info('set-piece seed skipped: %s', exc)
    # Collapse near-duplicate awards/takes within 3s
    rows.sort(key=lambda r: r['t_start_s'])
    deduped: list[dict] = []
    for r in rows:
        if deduped and abs(r['t_start_s'] - deduped[-1]['t_start_s']) < 3.0:
            continue
        deduped.append(r)
    return deduped
