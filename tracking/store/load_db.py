"""Load tracking aggregates into Postgres."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from db.connection import connect

logger = logging.getLogger(__name__)


def upsert_job(match_id: int, **fields: Any) -> None:
    cols = ['match_id'] + list(fields.keys())
    vals = [match_id] + list(fields.values())
    sets = ', '.join(f'{k} = EXCLUDED.{k}' for k in fields)
    placeholders = ', '.join(['%s'] * len(cols))
    sql = f"""
        INSERT INTO tracking_jobs ({', '.join(cols)})
        VALUES ({placeholders})
        ON CONFLICT (match_id) DO UPDATE SET {sets}, updated_at = now()
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
        conn.commit()


def replace_player_match(match_id: int, rows: list[dict]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM tracking_player_match WHERE match_id = %s', (match_id,))
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO tracking_player_match (
                        match_id, track_id, team, role, distance_m, sprint_count,
                        avg_x_m, avg_y_m, final_third_share, frames
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        match_id, r['track_id'], r.get('team'), r.get('role', 'player'),
                        r.get('distance_m'), r.get('sprint_count', 0),
                        r.get('avg_x_m'), r.get('avg_y_m'),
                        r.get('final_third_share'), r.get('frames', 0),
                    ),
                )
        conn.commit()


def replace_team_match(match_id: int, rows: list[dict]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM tracking_team_match WHERE match_id = %s', (match_id,))
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO tracking_team_match (
                        match_id, team, avg_line_height_m, avg_width_m, avg_compactness,
                        share_def_third, share_mid_third, share_att_third
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        match_id, r['team'], r.get('avg_line_height_m'),
                        r.get('avg_width_m'), r.get('avg_compactness'),
                        r.get('share_def_third'), r.get('share_mid_third'),
                        r.get('share_att_third'),
                    ),
                )
        conn.commit()


def replace_phases(match_id: int, rows: list[dict]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM tracking_clip_manifest WHERE match_id = %s', (match_id,))
            cur.execute('DELETE FROM match_phases WHERE match_id = %s', (match_id,))
            for r in rows:
                feats = r.get('features') or {}
                if not isinstance(feats, str):
                    feats = json.dumps(feats)
                cur.execute(
                    """
                    INSERT INTO match_phases (
                        match_id, team, phase, t_start_s, t_end_s, features
                    ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        match_id, r.get('team'), r['phase'],
                        r['t_start_s'], r['t_end_s'], feats,
                    ),
                )
        conn.commit()


def fetch_event_summary(match_id: int) -> dict[str, Any]:
    """Pull a few event-warehouse facts for the fused report."""
    out: dict[str, Any] = {'match_id': match_id}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.match_id, m.match_date, m.matchweek, m.home_score, m.away_score,
                           m.home_xg, m.away_xg, h.name AS home_team, a.name AS away_team
                    FROM matches m
                    JOIN teams h ON h.team_id = m.home_team_id
                    JOIN teams a ON a.team_id = m.away_team_id
                    WHERE m.match_id = %s
                    """,
                    (match_id,),
                )
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    out['match'] = dict(zip(cols, row))
                cur.execute(
                    """
                    SELECT t.name, ts.possession_pct, ts.shots, ts.shots_on_target
                    FROM match_team_stats ts
                    JOIN teams t ON t.team_id = ts.team_id
                    WHERE ts.match_id = %s
                    """,
                    (match_id,),
                )
                out['team_stats'] = [
                    dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()
                ]
    except Exception as exc:  # noqa: BLE001
        logger.warning('event summary unavailable: %s', exc)
        out['error'] = str(exc)
    return out


def utcnow():
    return datetime.now(timezone.utc)
