#!/usr/bin/env python3
"""Populate the derived tactical tables from `events`.

    python analytics/build.py --season '2025/2026'      # whole season
    python analytics/build.py --match 1903128           # one match
    python analytics/build.py --season '2025/2026' --force

Reads only from Postgres — the warehouse is the single source of truth, so
nothing here depends on `data/processed` still being on disk. Each match is
rebuilt inside one transaction and is idempotent: existing rows for the match
are deleted first, so a re-run replaces rather than duplicates.

Fills `possession_sequences`, `pressure_events` and `match_tactical_features`,
which are three of the dimensions `db/readiness.py` reports as missing.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg2.extras import RealDictCursor, execute_values  # noqa: E402

from analytics.carries import carries as derive_carries  # noqa: E402
from analytics.possession import segment, summarise_team  # noqa: E402
from analytics.pressing import pressure_events, team_pressing_features  # noqa: E402
from db.connection import club_schema, connect, current_schema  # noqa: E402

EVENT_SQL = """
    SELECT event_uid, event_index, period, minute, second, team_id, player_id,
           type, outcome, x, y, end_x, end_y, is_shot, is_goal, xt_delta
    FROM events
    WHERE match_id = %s
    ORDER BY event_index
"""


def load_events(cur, match_id: int) -> list[dict]:
    cur.execute(EVENT_SQL, (match_id,))
    return cur.fetchall()


def matches_for(cur, season: str | None, match_id: int | None) -> list[dict]:
    if match_id:
        cur.execute("""
            SELECT match_id, home_team_id, away_team_id FROM matches
            WHERE match_id = %s
        """, (match_id,))
    else:
        cur.execute("""
            SELECT m.match_id, m.home_team_id, m.away_team_id
            FROM matches m JOIN seasons s ON s.season_id = m.season_id
            WHERE (%s IS NULL OR s.name = %s)
            ORDER BY m.match_date
        """, (season, season))
    return cur.fetchall()


def build_match(cur, match: dict) -> dict:
    match_id = match['match_id']
    events = load_events(cur, match_id)
    if not events:
        return {'match_id': match_id, 'skipped': 'no events'}

    sequences = segment(events)
    pressures = pressure_events(events, sequences)
    ball_carries = derive_carries(events)

    cur.execute('DELETE FROM possession_sequences WHERE match_id = %s', (match_id,))
    cur.execute('DELETE FROM pressure_events WHERE match_id = %s', (match_id,))
    cur.execute('DELETE FROM carries WHERE match_id = %s', (match_id,))
    cur.execute('DELETE FROM match_tactical_features WHERE match_id = %s', (match_id,))

    seq_cols = ['sequence_index', 'team_id', 'period', 'start_event_uid',
                'end_event_uid', 'start_minute', 'start_second', 'end_minute',
                'end_second', 'duration_s', 'events', 'passes', 'players',
                'start_x', 'start_y', 'end_x', 'end_y', 'max_x', 'progression_m',
                'xt_sum', 'start_type', 'build_zone', 'is_high_turnover',
                'end_type', 'ended_in_shot', 'ended_in_goal']
    execute_values(cur, f"""
        INSERT INTO possession_sequences (match_id, {', '.join(seq_cols)})
        VALUES %s
    """, [[match_id] + [s[c] for c in seq_cols] for s in sequences])

    if pressures:
        pr_cols = ['event_uid', 'team_id', 'player_id', 'opponent_team_id',
                   'minute', 'second', 'period', 'type', 'outcome', 'x', 'y',
                   'third', 'is_counterpress', 'seconds_since_loss']
        execute_values(cur, f"""
            INSERT INTO pressure_events (match_id, {', '.join(pr_cols)})
            VALUES %s
        """, [[match_id] + [p[c] for c in pr_cols] for p in pressures])

    if ball_carries:
        ca_cols = ['event_uid', 'team_id', 'player_id', 'period', 'minute',
                   'second', 'start_x', 'start_y', 'end_x', 'end_y',
                   'distance_m', 'progression_m', 'duration_s', 'start_zone',
                   'end_zone', 'is_progressive', 'ends_in_shot']
        execute_values(cur, f"""
            INSERT INTO carries (match_id, {', '.join(ca_cols)})
            VALUES %s
            ON CONFLICT (match_id, event_uid) DO NOTHING
        """, [[match_id] + [c[k] for k in ca_cols] for c in ball_carries])

    # Two rows, one per team.
    rows = []
    for team_id, opponent_id, is_home in (
            (match['home_team_id'], match['away_team_id'], True),
            (match['away_team_id'], match['home_team_id'], False)):
        feats = {'match_id': match_id, 'team_id': team_id,
                 'opponent_team_id': opponent_id, 'is_home': is_home}
        feats.update(team_pressing_features(events, pressures, team_id, opponent_id))
        feats.update(summarise_team(sequences, team_id))
        feats['field_tilt_pct'] = _field_tilt(events, team_id, opponent_id)
        rows.append(feats)

    cols = list(rows[0].keys())
    execute_values(cur, f"""
        INSERT INTO match_tactical_features ({', '.join(cols)}) VALUES %s
    """, [[r[c] for c in cols] for r in rows])

    return {
        'match_id': match_id,
        'sequences': len(sequences),
        'pressures': len(pressures),
        'carries': len(ball_carries),
        'longest': max((s['events'] for s in sequences), default=0),
        'ppda': [r['ppda'] for r in rows],
    }


def _field_tilt(events, team_id, opponent_id) -> float | None:
    """Share of final-third touches belonging to this team.

    Recomputed here rather than read from match_team_stats so the tactical
    table stands on its own and can be rebuilt from `events` alone.
    """
    ours = sum(1 for e in events
               if e['team_id'] == team_id and (e['x'] or 0) >= 70)
    theirs = sum(1 for e in events
                 if e['team_id'] == opponent_id and (e['x'] or 0) >= 70)
    total = ours + theirs
    return round(100 * ours / total, 2) if total else None


def build(season: str | None = None, match_id: int | None = None,
          schema: str | None = None) -> dict:
    schema = schema or current_schema()
    started = time.time()
    done, skipped = [], []

    with connect(schema=schema) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        targets = matches_for(cur, season, match_id)
        print(f'{len(targets)} match(es) to build in schema {schema}')

        for n, match in enumerate(targets, 1):
            result = build_match(cur, match)
            if result.get('skipped'):
                skipped.append(result)
                print(f'  [{n}/{len(targets)}] {result["match_id"]} '
                      f'skipped: {result["skipped"]}')
            else:
                done.append(result)
                print(f'  [{n}/{len(targets)}] {result["match_id"]}  '
                      f'{result["sequences"]:4d} seq  '
                      f'{result["pressures"]:4d} press  '
                      f'{result["carries"]:4d} carries  '
                      f'longest {result["longest"]:2d}  '
                      f'PPDA {result["ppda"]}', flush=True)
            conn.commit()

    total_seq = sum(d['sequences'] for d in done)
    print(f'\nbuilt {len(done)} match(es) in {time.time() - started:.1f}s: '
          f'{total_seq:,} sequences, '
          f'{sum(d["pressures"] for d in done):,} pressure events, '
          f'{sum(d["carries"] for d in done):,} carries')
    if skipped:
        print(f'skipped {len(skipped)}')
    return {'built': len(done), 'skipped': len(skipped), 'sequences': total_seq}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--season', default=None)
    ap.add_argument('--match', type=int, default=None)
    ap.add_argument('--club', default=None)
    a = ap.parse_args(argv)

    schema = club_schema(a.club) if a.club else current_schema()
    build(season=a.season, match_id=a.match, schema=schema)
    return 0


if __name__ == '__main__':
    sys.exit(main())
