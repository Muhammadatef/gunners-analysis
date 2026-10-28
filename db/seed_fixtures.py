#!/usr/bin/env python3
"""Seed / refresh the season calendar in Postgres from the fixtures JSON.

    python db/seed_fixtures.py                       # default 2026/27 file
    python db/seed_fixtures.py --file <path.json>

Safe to re-run: an existing fixture keeps its status, resolved match id and
attempt count; only calendar fields (date, kick-off, confirmation) are refreshed
so a TV reschedule flows straight through to the DAGs.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db.connection import connect  # noqa: E402

DEFAULT_FILE = os.path.join(ROOT, 'airflow', 'fixtures', 'arsenal_2026_27.json')


def seed(path: str = DEFAULT_FILE) -> int:
    with open(path) as fh:
        data = json.load(fh)

    fixtures = data['fixtures']
    dates = sorted(f['date'] for f in fixtures)

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO seasons (name, competition, start_date, end_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE
              SET competition = EXCLUDED.competition,
                  start_date = EXCLUDED.start_date,
                  end_date = EXCLUDED.end_date
            RETURNING season_id
        """, (data['season'], data['competition'], dates[0], dates[-1]))
        season_id = cur.fetchone()[0]

        for f in fixtures:
            match_id = f.get('whoscored_match_id')
            match_url = None
            if match_id:
                match_url = (
                    f"https://www.whoscored.com/matches/{match_id}/live/"
                    f"{f['whoscored_slug']}"
                )
            cur.execute("""
                INSERT INTO fixtures (season_id, fixture_key, matchweek, scheduled_date,
                    kickoff_uk, kickoff_confirmed, home_team_name, away_team_name,
                    opponent, venue, whoscored_slug, whoscored_match_id, match_url,
                    status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s IS NOT NULL THEN 'ready'::fixture_status
                             ELSE 'scheduled'::fixture_status END)
                ON CONFLICT (fixture_key) DO UPDATE SET
                    scheduled_date = EXCLUDED.scheduled_date,
                    kickoff_uk = EXCLUDED.kickoff_uk,
                    kickoff_confirmed = EXCLUDED.kickoff_confirmed,
                    whoscored_slug = EXCLUDED.whoscored_slug,
                    whoscored_match_id = COALESCE(EXCLUDED.whoscored_match_id,
                                                  fixtures.whoscored_match_id),
                    match_url = COALESCE(EXCLUDED.match_url, fixtures.match_url),
                    status = CASE
                        WHEN EXCLUDED.whoscored_match_id IS NOT NULL
                             AND fixtures.status IN ('scheduled', 'resolving')
                        THEN 'ready'::fixture_status
                        ELSE fixtures.status
                    END
            """, (season_id, f['fixture_key'], f['matchweek'], f['date'],
                  f['kickoff_uk'], f['kickoff_confirmed'], f['home_team'], f['away_team'],
                  f['opponent'], f['venue'], f['whoscored_slug'], match_id, match_url,
                  match_id))

    print(f'seeded {len(fixtures)} fixtures for {data["season"]} '
          f'({dates[0]} .. {dates[-1]})')
    return len(fixtures)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', default=DEFAULT_FILE)
    a = ap.parse_args()
    seed(a.file)
