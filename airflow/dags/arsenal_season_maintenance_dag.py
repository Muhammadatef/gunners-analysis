"""Weekly housekeeping for the Arsenal season pipeline.

Runs every Monday 06:00 UK and:

  1. re-seeds the fixture calendar into Postgres (picks up TV reschedules)
  2. resolves WhoScored match ids for anything kicking off in the next 21 days
  3. re-runs any fixture that is past kick-off but still not ingested
  4. writes a season progress summary to the task log

This is the safety net: even if a per-fixture DAG misfires (postponement,
WhoScored outage, a bot wall), the backfill task picks the match up here.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pendulum
from airflow.decorators import dag, task

PROJECT_ROOT = os.environ.get('GUNNERS_ROOT', '/opt/gunners')
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

UK = pendulum.timezone('Europe/London')
LOOKAHEAD_DAYS = 21
SEASON = '2026/2027'


@dag(
    dag_id='arsenal_season_maintenance',
    description='Refresh the Arsenal fixture calendar, resolve match ids, backfill misses',
    schedule='0 6 * * 1',
    start_date=pendulum.datetime(2026, 8, 1, tz=UK),
    catchup=False,
    max_active_runs=1,
    default_args={'owner': 'gunners', 'retries': 2, 'retry_delay': timedelta(minutes=10)},
    tags=['arsenal', SEASON, 'maintenance'],
)
def maintenance():

    @task
    def ensure_schema() -> str:
        from db.connection import apply_schema, dsn
        apply_schema(os.path.join(PROJECT_ROOT, 'db', 'schema.sql'))
        return dsn().rsplit('@', 1)[-1]

    @task
    def reseed_calendar() -> int:
        from db.seed_fixtures import seed
        return seed(os.path.join(PROJECT_ROOT, 'airflow', 'fixtures', 'arsenal_2026_27.json'))

    @task
    def resolve_upcoming() -> list[dict]:
        """Look up WhoScored ids for fixtures inside the lookahead window."""
        from scraper.fixtures import resolve_match_id, match_url
        from scraper.scraper import WhoscoredScraper
        from db.connection import connect

        horizon = pendulum.now(UK).add(days=LOOKAHEAD_DAYS).date()
        with connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT fixture_key, whoscored_slug, scheduled_date
                  FROM fixtures
                 WHERE whoscored_match_id IS NULL
                   AND status IN ('scheduled', 'resolving')
                   AND scheduled_date <= %s
                 ORDER BY scheduled_date
            """, (horizon,))
            pending = cur.fetchall()

        if not pending:
            return []

        resolved = []
        scraper = WhoscoredScraper()
        try:
            for key, slug, date in pending:
                match_id = resolve_match_id(slug, scraper=scraper)
                if not match_id:
                    continue
                with connect() as conn:
                    conn.cursor().execute("""
                        UPDATE fixtures
                           SET whoscored_match_id = %s, match_url = %s, status = 'ready'
                         WHERE fixture_key = %s
                    """, (match_id, match_url(match_id, slug), key))
                resolved.append({'fixture_key': key, 'match_id': match_id, 'date': str(date)})
        finally:
            scraper.close()

        print(f'resolved {len(resolved)}/{len(pending)} pending fixtures')
        return resolved

    @task
    def backfill_missed() -> list[str]:
        """Re-run the pipeline for any match that has kicked off but never landed."""
        import uuid
        from db.connection import connect
        from orchestrator import pipeline
        from scraper.scraper import WhoscoredScraper

        cutoff = pendulum.now(UK).subtract(hours=6).date()
        with connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT fixture_key, match_url, attempts
                  FROM fixtures
                 WHERE status <> 'ingested'
                   AND status <> 'postponed'
                   AND match_url IS NOT NULL
                   AND scheduled_date <= %s
                   AND attempts < 5
                 ORDER BY scheduled_date
            """, (cutoff,))
            missed = cur.fetchall()

        done = []
        for key, url, attempts in missed:
            job_id = str(uuid.uuid4())
            try:
                with connect() as conn:
                    conn.cursor().execute(
                        "UPDATE fixtures SET status = 'ingesting', attempts = attempts + 1 "
                        "WHERE fixture_key = %s", (key,))

                html = os.path.join(PROJECT_ROOT, 'data', 'raw', f'match_{job_id}.html')
                s = WhoscoredScraper()
                try:
                    s.scrape_match(url, html, wait_time=15)
                finally:
                    s.close()
                pipeline.extract_match_json(html)
                pipeline.run_transform(job_id, season=SEASON)
                report = pipeline.run_quality(job_id)
                if not report['passed']:
                    raise ValueError(report['summary'])
                out = pipeline.run_visualize(job_id)
                pipeline.run_load(job_id, SEASON, fixture_key=key, out_dir=out)
                done.append(key)
            except Exception as exc:                              # noqa: BLE001
                with connect() as conn:
                    conn.cursor().execute(
                        "UPDATE fixtures SET status = 'failed', last_error = %s "
                        "WHERE fixture_key = %s", (str(exc)[:2000], key))
                print(f'backfill failed for {key}: {exc}')

        print(f'backfilled {len(done)}/{len(missed)}')
        return done

    @task
    def season_report() -> dict:
        from db.connection import connect

        with connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT status, COUNT(*) FROM fixtures GROUP BY status ORDER BY status
            """)
            by_status = dict(cur.fetchall())
            cur.execute("""
                SELECT played, won, drawn, lost, goals_for, goals_against, points,
                       avg_possession, total_xt
                  FROM v_team_season_summary WHERE team_id = 13
            """)
            row = cur.fetchone()

        summary = {'fixtures_by_status': by_status}
        if row:
            played, w, d, l, gf, ga, pts, poss, xt = row
            summary['arsenal'] = {
                'played': played, 'w': w, 'd': d, 'l': l,
                'goals_for': gf, 'goals_against': ga, 'points': pts,
                'avg_possession': float(poss) if poss is not None else None,
                'total_xt': float(xt) if xt is not None else None,
            }
            print(f'Arsenal {played}pl {w}W {d}D {l}L  {gf}:{ga}  {pts}pts')
        print(f'fixtures: {by_status}')
        return summary

    schema = ensure_schema()
    seeded = reseed_calendar()
    schema >> seeded >> resolve_upcoming() >> backfill_missed() >> season_report()


maintenance()
