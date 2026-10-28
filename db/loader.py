#!/usr/bin/env python3
"""Load one processed match into the Postgres warehouse.

    python db/loader.py --job <job_id> [--season 2026/2027] [--fixture-key gw17_...]
                        [--club barcelona]

Idempotent: re-running the same job replaces that match's rows. `--club` picks
the schema; without it the load lands in `gunners` exactly as it always has.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime

from psycopg2.extras import Json, execute_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import aggregate                                    # noqa: E402
from db.connection import club_schema, connect              # noqa: E402
from visualizer import match_report                         # noqa: E402

# Club colours live in visualizer/palette.py, next to the code that decides
# whether the two teams in a match can be told apart. Re-exported here because
# this is where callers have always imported them from.
from visualizer.palette import TEAM_COLORS, team_color  # noqa: E402,F401


# ---------------------------------------------------------------------------

def upsert_season(cur, name: str, competition: str | None = None) -> int:
    """Upsert the season row.

    `competition` is set when the row is first created and left alone after
    that. A club plays in several competitions in one season - the accurate
    per-match value is `matches.competition`, and letting each load rewrite
    seasons.competition would just make it flip-flop.
    """
    if competition:
        cur.execute("""
            INSERT INTO seasons (name, competition) VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING season_id
        """, (name, competition))
    else:
        cur.execute("""
            INSERT INTO seasons (name) VALUES (%s)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING season_id
        """, (name,))
    return cur.fetchone()[0]


def upsert_teams(cur, M):
    for tid, name in M.teams_dict.items():
        color = team_color(name)
        slug = name.lower().replace(' & ', '-').replace(' ', '-')
        cur.execute("""
            INSERT INTO teams (team_id, name, slug, primary_color)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (team_id) DO UPDATE
              SET name = EXCLUDED.name,
                  slug = COALESCE(EXCLUDED.slug, teams.slug),
                  primary_color = COALESCE(teams.primary_color, EXCLUDED.primary_color)
        """, (int(tid), name, slug, color))


def upsert_players(cur, player_rows, season_id, match_date):
    execute_values(cur, """
        INSERT INTO players (player_id, name, short_name, height_cm, weight_kg)
        VALUES %s
        ON CONFLICT (player_id) DO UPDATE
          SET name = EXCLUDED.name,
              short_name = EXCLUDED.short_name,
              height_cm = COALESCE(EXCLUDED.height_cm, players.height_cm),
              weight_kg = COALESCE(EXCLUDED.weight_kg, players.weight_kg)
    """, [(r['player_id'], r['name'], match_report.get_short_name(r['name']),
           r['height_cm'], r['weight_kg']) for r in player_rows])

    execute_values(cur, """
        INSERT INTO player_team_spells (player_id, team_id, season_id, first_seen, last_seen)
        VALUES %s
        ON CONFLICT (player_id, team_id, season_id) DO UPDATE
          SET first_seen = LEAST(player_team_spells.first_seen, EXCLUDED.first_seen),
              last_seen  = GREATEST(player_team_spells.last_seen, EXCLUDED.last_seen)
    """, [(r['player_id'], r['team_id'], season_id, match_date, match_date) for r in player_rows])


def load(job_id: str, season_name: str, fixture_key: str | None = None,
         out_dir: str | None = None, dag_run_id: str | None = None,
         club: str | None = None) -> int:
    started = datetime.utcnow()
    M = match_report.prepare(job_id)

    mrow = aggregate.match_row(M)
    match_id = mrow['match_id']
    trows = aggregate.team_rows(M)
    prows = aggregate.player_rows(M)
    erows = aggregate.event_rows(M)
    srows = aggregate.shot_rows(M)
    lrows = aggregate.pass_link_rows(M)

    run_id = str(uuid.uuid4())

    # schema= rather than the ambient contextvar: this is the one writer in the
    # codebase, and it must never depend on which thread it happens to run on.
    with connect(schema=club) as conn:
        cur = conn.cursor()
        season_id = upsert_season(cur, season_name, mrow.get('competition'))
        upsert_teams(cur, M)

        fixture_id = None
        if fixture_key:
            cur.execute("SELECT fixture_id FROM fixtures WHERE fixture_key = %s", (fixture_key,))
            got = cur.fetchone()
            fixture_id = got[0] if got else None

        cur.execute("""
            INSERT INTO pipeline_runs (job_id, fixture_id, dag_run_id, stage, status, started_at)
            VALUES (%s, %s, %s, 'load', 'running', %s)
        """, (run_id, fixture_id, dag_run_id, started))

        # --- match ------------------------------------------------------
        cur.execute("""
            INSERT INTO matches (match_id, fixture_id, season_id, competition, matchweek,
                kickoff_utc, match_date, home_team_id, away_team_id, home_score, away_score,
                ht_home_score, ht_away_score, home_xg, away_xg, home_xgot, away_xgot, xg_source,
                venue_name, referee, attendance, max_minute, event_count, job_id)
            VALUES (%(match_id)s, %(fixture_id)s, %(season_id)s, %(competition)s, %(matchweek)s,
                %(kickoff_utc)s, %(match_date)s, %(home_team_id)s, %(away_team_id)s,
                %(home_score)s, %(away_score)s, %(ht_home_score)s, %(ht_away_score)s,
                %(home_xg)s, %(away_xg)s, %(home_xgot)s, %(away_xgot)s, %(xg_source)s,
                %(venue_name)s, %(referee)s, %(attendance)s, %(max_minute)s, %(event_count)s,
                %(job_id)s)
            ON CONFLICT (match_id) DO UPDATE SET
                fixture_id = EXCLUDED.fixture_id, season_id = EXCLUDED.season_id,
                matchweek = EXCLUDED.matchweek, home_score = EXCLUDED.home_score,
                away_score = EXCLUDED.away_score, ht_home_score = EXCLUDED.ht_home_score,
                ht_away_score = EXCLUDED.ht_away_score, home_xg = EXCLUDED.home_xg,
                away_xg = EXCLUDED.away_xg, home_xgot = EXCLUDED.home_xgot,
                away_xgot = EXCLUDED.away_xgot, xg_source = EXCLUDED.xg_source,
                venue_name = EXCLUDED.venue_name, referee = EXCLUDED.referee,
                attendance = EXCLUDED.attendance, max_minute = EXCLUDED.max_minute,
                event_count = EXCLUDED.event_count, job_id = EXCLUDED.job_id,
                ingested_at = now()
        """, {**mrow, 'fixture_id': fixture_id, 'season_id': season_id,
              'job_id': job_id if _is_uuid(job_id) else None})

        upsert_players(cur, prows, season_id, mrow['match_date'])

        # --- replace child rows (idempotent re-ingest) -------------------
        for table in ('pass_links', 'shots', 'events', 'match_player_stats', 'match_team_stats'):
            cur.execute(f'DELETE FROM {table} WHERE match_id = %s', (match_id,))

        _insert(cur, 'match_team_stats', trows, match_id)
        _insert(cur, 'match_player_stats',
                [{k: v for k, v in r.items()
                  if k not in ('name', 'height_cm', 'weight_kg', 'age')} for r in prows],
                match_id)

        ev_cols = list(erows[0].keys())

        def _cell(row, col):
            # An absent qualifier list is SQL NULL, never a JSON `null`: the
            # column's health is measured with jsonb_typeof, and a jsonb null
            # would read as a third kind alongside array and string.
            if col != 'qualifiers':
                return row[col]
            return Json(row[col]) if row[col] is not None else None

        execute_values(cur, f"""
            INSERT INTO events (match_id, {', '.join(ev_cols)})
            VALUES %s
        """, [[match_id] + [_cell(r, c) for c in ev_cols] for r in erows])

        # map event_index -> event_uid so shots can point at their event
        cur.execute('SELECT event_index, event_uid FROM events WHERE match_id = %s', (match_id,))
        uid_by_index = dict(cur.fetchall())
        for s in srows:
            s['event_uid'] = uid_by_index.get(s.pop('event_index'))
        _insert(cur, 'shots', srows, match_id)
        _insert(cur, 'pass_links', lrows, match_id)

        # --- quality report + rendered panels ---------------------------
        qpath = os.path.join(ROOT, 'data', 'quality', f'{job_id}_report.json')
        if os.path.exists(qpath):
            with open(qpath) as fh:
                report = json.load(fh)
            # quality/checks.py reports `checks` as a LIST of check dicts and
            # carries its own totals. Treating it as a dict silently produced
            # checks_total=NULL, checks_failed=0 and passed=True for every
            # report - including failing ones. Trust the report's own fields,
            # and only fall back to counting when they are absent.
            checks = report.get('checks') or []
            if isinstance(checks, dict):                       # legacy shape
                checks = [{'passed': v} for v in checks.values()]
            failed = report.get('checks_failed')
            if failed is None:
                failed = sum(1 for c in checks
                             if isinstance(c, dict)
                             and not c.get('passed', True)
                             and c.get('severity', 'error') == 'error')
            total = report.get('checks_total') or len(checks) or None
            passed = report.get('passed')
            if passed is None:
                passed = failed == 0

            cur.execute("""
                INSERT INTO quality_reports (job_id, match_id, passed, checks_total, checks_failed, report)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (run_id, match_id, bool(passed), total, int(failed), Json(report)))

        out_dir = out_dir or os.path.join(ROOT, 'final_output')
        if os.path.isdir(out_dir):
            cur.execute('DELETE FROM visualizations WHERE match_id = %s', (match_id,))
            rows = []
            for fname in sorted(os.listdir(out_dir)):
                if not fname.endswith('.png'):
                    continue
                fpath = os.path.join(out_dir, fname)
                rows.append((match_id, run_id, fname[:-4], _panel_title(fname),
                             fpath, None, None, os.path.getsize(fpath)))
            if rows:
                execute_values(cur, """
                    INSERT INTO visualizations
                        (match_id, job_id, panel, title, file_path, width_px, height_px, bytes)
                    VALUES %s
                """, rows)

        if fixture_id:
            cur.execute("""
                UPDATE fixtures SET status = 'ingested', whoscored_match_id = %s, last_error = NULL
                WHERE fixture_id = %s
            """, (match_id, fixture_id))

        cur.execute("""
            UPDATE pipeline_runs
               SET match_id = %s, status = 'success', finished_at = now(),
                   duration_s = EXTRACT(EPOCH FROM (now() - started_at))
             WHERE job_id = %s
        """, (match_id, run_id))

    print(f'loaded match {match_id}: {mrow["home_score"]}-{mrow["away_score"]}  '
          f'{len(erows)} events, {len(prows)} players, {len(srows)} shots, {len(lrows)} pass links')
    return match_id


def _insert(cur, table, rows, match_id):
    if not rows:
        return
    cols = list(rows[0].keys())
    execute_values(cur, f"INSERT INTO {table} (match_id, {', '.join(cols)}) VALUES %s",
                   [[match_id] + [r[c] for c in cols] for r in rows])


def _panel_title(fname: str) -> str:
    return fname[3:-4].replace('_', ' ').title()


def _is_uuid(v) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', required=True)
    ap.add_argument('--season', default='2026/2027')
    ap.add_argument('--fixture-key', default=None)
    ap.add_argument('--out-dir', default=None)
    ap.add_argument('--dag-run-id', default=None)
    ap.add_argument('--club', default=None,
                    help="club schema to load into ('barcelona'); default gunners")
    a = ap.parse_args()
    load(a.job, a.season, a.fixture_key, a.out_dir, a.dag_run_id,
         club=club_schema(a.club) if a.club else None)
