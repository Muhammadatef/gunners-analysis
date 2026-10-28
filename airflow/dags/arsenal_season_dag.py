"""Dynamically generate one Airflow DAG per Arsenal fixture in 2026/27.

Each DAG fires once, a few hours after its own kick-off, and runs the whole
pipeline end to end:

    await_final_whistle -> resolve_match_id -> scrape -> transform -> enrich_xg
      -> validate -> visualize -> load_warehouse
      -> football_analyst_DSPy_agent_match_report

Scheduling
----------
Every fixture gets a cron pinned to its own date and time, bracketed by
`start_date` / `end_date` so it can only ever fire for that one match. Kick-off
times that the Premier League has not confirmed yet default to 15:00 UK; re-run
`airflow/fixtures/build_fixtures.py` + `db/seed_fixtures.py` when the TV picks
land and the DAGs pick the new slot up on the next parse.

The run happens POST_MATCH_DELAY_MINUTES after the *final whistle* (default 30).
The whistle itself is derived, not known: kick-off + MATCH_DURATION_MINUTES
(default 115 = 90 + a 15-minute interval + ~10 of stoppage), so the default
schedule is kick-off + 2h25.

IMPORTANT: the schedule uses CronTriggerTimetable (not a plain cron string).
A bare yearly cron like `25 22 21 8 *` is treated as a year-long *data interval*
whose DagRun is only created at the *end* of the interval — i.e. next August.
CronTriggerTimetable fires at the cron instant so MW2/MW3/… actually run.

30 minutes is tight against WhoScored's own processing lag - the match centre is
not always populated the moment the whistle goes. The tasks retry 6 times at
30-minute intervals, `end_date` keeps the DagRun-creation window open for three
days, and the maintenance DAG backfills anything that still missed. Raise
POST_MATCH_DELAY_MINUTES if the first attempt fails often enough to be noise.

Waiting for the whistle
-----------------------
A guessed kick-off makes the cron a guess too, and no retry budget on
`resolve_match_id` can rescue a DAG that fired before the match: MW4 kicked off
at 20:00, the calendar said 15:00, and the run happened at 17:25. So the first
task is `await_final_whistle`, which asks FotMob's unauthenticated feed whether
the match has actually finished and retries for six hours if it has not.

Failures here retry deliberately. `AirflowSkipException` is a *skip*: it ends
the run and no `retries` setting applies to it, which is why the old
"WhoScored has not published a match id yet" skip was terminal rather than the
patient wait it read as. It is now reserved for the two cases that really are
nothing to wait for - a cancelled fixture, and an analyst report that is
explicitly optional.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowSkipException
from airflow.models import Variable
from airflow.timetables.trigger import CronTriggerTimetable

PROJECT_ROOT = os.environ.get('GUNNERS_ROOT', '/opt/gunners')
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

FIXTURES_FILE = os.path.join(PROJECT_ROOT, 'airflow', 'fixtures', 'arsenal_2026_27.json')
UK = pendulum.timezone('Europe/London')
ANALYST_URL = os.environ.get('ANALYST_URL', 'http://analyst:8100')
COMPETITION = 'Premier League'

DEFAULT_KICKOFF = '15:00'

# 90 minutes of football, a 15-minute interval, and ~10 of stoppage across both
# halves. Extra time is not modelled - a cup tie that goes to 120 minutes simply
# runs early and retries.
MATCH_DURATION_MINUTES = int(os.environ.get('MATCH_DURATION_MINUTES', '115'))
POST_MATCH_DELAY_MINUTES = int(os.environ.get('POST_MATCH_DELAY_MINUTES', '30'))

# Superseded by the two settings above. Still honoured so an existing
# PIPELINE_DELAY_HOURS in a .env keeps working: it sets the total offset from
# kick-off, exactly as it used to.
_LEGACY_DELAY_HOURS = os.environ.get('PIPELINE_DELAY_HOURS')
DELAY_FROM_KICKOFF_MINUTES = (
    int(float(_LEGACY_DELAY_HOURS) * 60) if _LEGACY_DELAY_HOURS
    else MATCH_DURATION_MINUTES + POST_MATCH_DELAY_MINUTES)

# await_final_whistle polls a schedule that is mostly guesswork, so its retry
# budget has to cover being wrong about kick-off by most of an afternoon:
# 24 x 15 min = 6h, enough for a 17:25 firing to reach a 20:00 kick-off's
# whistle. Every other task keeps the tighter DEFAULT_ARGS budget.
WHISTLE_RETRIES = int(os.environ.get('WHISTLE_RETRIES', '24'))
WHISTLE_RETRY_DELAY = timedelta(
    minutes=int(os.environ.get('WHISTLE_RETRY_DELAY_MINUTES', '15')))

DEFAULT_ARGS = {
    'owner': 'gunners',
    'retries': 6,
    'retry_delay': timedelta(minutes=30),
    'retry_exponential_backoff': False,
    'email_on_failure': False,
    'depends_on_past': False,
}


def load_fixtures() -> dict:
    with open(FIXTURES_FILE) as fh:
        return json.load(fh)


def remember_match_id(fixture_key: str, match_id: int) -> None:
    """Write a resolved id back into the season calendar.

    Postgres already records it, but the calendar is what seeds a fresh
    database and what `backfill_season` reads. Keeping only one of the two up
    to date is why MW2 and MW3 ended up hand-patched.

    Best effort, and it really is best effort: the repo is bind-mounted from
    the host, where it belongs to the developer, while the container runs as
    AIRFLOW_UID (50000). Unless the fixtures directory is writable by that uid
    this logs and moves on - Postgres is the source of truth the pipeline
    actually reads, and a resolved id is never lost by this failing.
    """
    try:
        data = load_fixtures()
        for entry in data['fixtures']:
            if entry['fixture_key'] == fixture_key:
                if entry.get('whoscored_match_id') == match_id:
                    return
                entry['whoscored_match_id'] = match_id
                break
        else:
            return
        tmp = f'{FIXTURES_FILE}.tmp'
        with open(tmp, 'w') as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, FIXTURES_FILE)
        print(f'calendar updated: {fixture_key} -> {match_id}')
    except OSError as exc:
        print(f'could not write match id back to the calendar ({exc}); the id is '
              f'safe in Postgres. To enable the write-back, make '
              f'airflow/fixtures writable by AIRFLOW_UID '
              f'(chown -R :{os.environ.get("AIRFLOW_UID", "50000")} airflow/fixtures '
              f'&& chmod -R g+w airflow/fixtures).')
    except Exception as exc:                                     # noqa: BLE001
        print(f'could not write match id back to the calendar: {exc}')


def final_whistle(kickoff: pendulum.DateTime) -> pendulum.DateTime:
    """When the match is expected to end. Derived - no feed tells us."""
    return kickoff.add(minutes=MATCH_DURATION_MINUTES)


def run_window(fixture: dict) -> tuple[pendulum.DateTime, str]:
    """Kick-off (UK) and the cron for 30 minutes after the final whistle."""
    ko_time = fixture.get('kickoff_uk') or DEFAULT_KICKOFF
    hh, mm = (int(x) for x in ko_time.split(':'))
    y, mo, d = (int(x) for x in fixture['date'].split('-'))
    kickoff = pendulum.datetime(y, mo, d, hh, mm, tz=UK)
    run_at = kickoff.add(minutes=DELAY_FROM_KICKOFF_MINUTES)
    cron = f'{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *'
    return kickoff, cron


def build_dag(fixture: dict, season: str):
    kickoff, cron = run_window(fixture)
    run_at = kickoff.add(minutes=DELAY_FROM_KICKOFF_MINUTES)
    key = fixture['fixture_key']
    dag_id = f'arsenal_{season.replace("/", "_")}_{key}'

    @dag(
        dag_id=dag_id,
        description=(f'MW{fixture["matchweek"]} {fixture["home_team"]} v '
                     f'{fixture["away_team"]} ({fixture["date"]}) - scrape, visualise, '
                     f'load. Runs {POST_MATCH_DELAY_MINUTES}min after the '
                     f'{final_whistle(kickoff).format("HH:mm")} final whistle'),
        # Trigger at the cron instant (not end of a year-long data interval).
        schedule=CronTriggerTimetable(cron, timezone=UK),
        start_date=run_at.subtract(days=1),
        end_date=run_at.add(days=3),
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULT_ARGS,
        tags=['arsenal', season, f'mw{fixture["matchweek"]:02d}',
              'home' if fixture['venue'] == 'H' else 'away', fixture['opponent']],
        params={'fixture': fixture, 'season': season},
    )
    def match_pipeline():

        @task(retries=WHISTLE_RETRIES, retry_delay=WHISTLE_RETRY_DELAY)
        def await_final_whistle() -> dict:
            """Block until the match has actually been played.

            The cron is pinned to a kick-off the calendar mostly guesses -
            32 of the 38 2026/27 fixtures have no confirmed time, so
            `run_window` assumes DEFAULT_KICKOFF. MW4 kicked off at 20:00 and
            this DAG fired at 17:25, two and a half hours early, which is not a
            case any amount of match-id retrying can fix.

            FotMob answers unauthenticated with the real kick-off and a
            full-time flag, so the schedule can stay a rough guess and this
            task holds the line. Raising a plain exception matters: it retries,
            where AirflowSkipException ends the run outright.
            """
            from scraper.fotmob import FotMobClient

            status = FotMobClient().match_status(
                fixture['date'], fixture['home_team'], fixture['away_team'],
                league=COMPETITION)

            if status is None:
                raise RuntimeError(
                    f'{fixture["home_team"]} v {fixture["away_team"]} is not listed '
                    f'for {fixture["date"]}; postponed, moved, or the calendar date '
                    f'is wrong')

            scheduled = kickoff.format('YYYY-MM-DD HH:mm')
            actual = status.get('utc_kickoff')
            if actual:
                actual = pendulum.parse(actual).in_timezone(UK).format('YYYY-MM-DD HH:mm')
                if actual != scheduled:
                    print(f'kick-off was {actual} UK, calendar said {scheduled} '
                          f'- refresh kickoff_uk for {key}')

            if status.get('cancelled'):
                raise AirflowSkipException(
                    f'{key} was cancelled: {status.get("reason")}')
            if not status.get('finished'):
                state = 'in progress' if status.get('started') else 'not kicked off'
                raise RuntimeError(
                    f'{key} is {state} (kick-off {actual or "unknown"} UK); waiting')

            print(f'full time: {status.get("score")} - {key}')
            return status

        @task
        def resolve_match_id(status: dict) -> str:
            """Turn the fixture into a live WhoScored match URL.

            Four sources, cheapest first. The scrape used to be the only one
            that ran, so an id already sitting in the calendar or in Postgres
            bought this DAG nothing and every run gambled on getting past
            WhoScored's bot wall.
            """
            from scraper.fixtures import (resolve_match_id as resolve, match_url,
                                          FixtureLookupError)
            from db.connection import connect

            slug = fixture['whoscored_slug']

            def from_variable():
                raw = Variable.get(f'match_id_{key}', default_var=None)
                return int(raw) if raw else None

            def from_calendar():
                return fixture.get('whoscored_match_id')

            def from_warehouse():
                with connect() as conn:
                    cur = conn.cursor()
                    cur.execute('SELECT whoscored_match_id FROM fixtures '
                                'WHERE fixture_key = %s', (key,))
                    row = cur.fetchone()
                return row[0] if row else None

            match_id = None
            for label, source in (('airflow variable', from_variable),
                                  ('season calendar', from_calendar),
                                  ('warehouse', from_warehouse),
                                  ('whoscored', lambda: resolve(slug))):
                try:
                    match_id = source()
                except FixtureLookupError as exc:
                    # Could not look, as opposed to nothing to find. Retry.
                    raise RuntimeError(f'WhoScored lookup failed for {slug}: {exc}')
                if match_id:
                    print(f'match id {match_id} from {label}')
                    break

            if not match_id:
                # await_final_whistle already proved the match was played, so a
                # missing id is a fault to retry, not a fixture to wait for.
                raise RuntimeError(
                    f'no WhoScored match id for {slug}; the match finished '
                    f'({status.get("score")}) so this is a resolution failure')

            match_id = int(match_id)
            url = match_url(match_id, slug)
            with connect() as conn:
                conn.cursor().execute(
                    "UPDATE fixtures SET whoscored_match_id = %s, match_url = %s, "
                    "status = 'ready' WHERE fixture_key = %s", (match_id, url, key))
            remember_match_id(key, match_id)
            return url

        @task
        def scrape(match_url: str) -> str:
            """Pull the match-centre HTML/JSON and return the job id."""
            import uuid
            from scraper.scraper import WhoscoredScraper
            from orchestrator.pipeline import extract_match_json

            job_id = str(uuid.uuid4())
            raw_html = os.path.join(PROJECT_ROOT, 'data', 'raw', f'match_{job_id}.html')
            s = WhoscoredScraper()
            try:
                s.scrape_match(match_url, raw_html, wait_time=15)
            finally:
                s.close()
            extract_match_json(raw_html)          # writes match_<job>.json next to it
            return job_id

        @task
        def transform(job_id: str) -> str:
            from orchestrator.pipeline import run_transform
            run_transform(job_id, fixture=fixture, season=season)
            return job_id

        @task
        def enrich_xg(job_id: str) -> str:
            """Add xG/xGOT from SofaScore - WhoScored's feed has none.

            Best effort: a failure here leaves the placeholder values and is
            recorded in metadata.xg_source rather than failing the run.
            """
            from orchestrator.pipeline import enrich_xg as enrich
            meta = enrich(job_id)
            print(f"xg_source={meta.get('xg_source')} "
                  f"xG {meta.get('home_xg')}-{meta.get('away_xg')}")
            return job_id

        @task
        def validate(job_id: str) -> str:
            from orchestrator.pipeline import run_quality
            report = run_quality(job_id)
            if not report.get('passed', True):
                raise ValueError(f'quality gate failed for {job_id}: {report}')
            return job_id

        @task
        def visualize(job_id: str) -> dict:
            """Render into output/<season>/mw<NN>_<date>_<home>_v_<away>/."""
            from visualizer import match_report
            result = match_report.render_match(
                job_id, season=season, matchweek=fixture['matchweek'])
            print(f'rendered -> {result["match_dir"]}')
            return {'job_id': job_id, 'match_dir': result['match_dir']}

        @task
        def load_warehouse(rendered: dict) -> dict:
            from db.loader import load
            mid = load(rendered['job_id'], season_name=season, fixture_key=key,
                       out_dir=os.path.join(rendered['match_dir'], 'panels'))
            return {
                'match_id': mid,
                'job_id': rendered['job_id'],
                'match_dir': rendered['match_dir'],
            }

        def _match_dir_from_fixture() -> str:
            from visualizer import output_layout
            root = output_layout.club_root()
            season_dir = output_layout.season_dir_name(season)
            name = output_layout.match_dir_name(
                fixture['date'], fixture['home_team'], fixture['away_team'],
                fixture.get('matchweek'))
            return os.path.join(root, season_dir, name)

        @task(task_id='football_analyst_DSPy_agent_match_report',
              doc_md='DSPy football analyst agent match report',
              retries=2,
              retry_delay=timedelta(minutes=5))
        def football_analyst_DSPy_agent_match_report(
                payload, rendered: dict | None = None) -> str:
            """DSPy football analyst agent match report.

            Panel-grounded technical REPORT.md for this fixture. Calls the
            analyst service over HTTP so dspy stays out of the Airflow image.

            `payload` is the load_warehouse XCom. Older runs stored a bare
            match_id int; current runs store {match_id, match_dir, job_id}.
            `rendered` is the visualize XCom and supplies match_dir on reruns.
            """
            import json
            import urllib.request

            optional = os.environ.get('GUNNERS_REPORT_OPTIONAL', '').strip() in (
                '1', 'true', 'yes')
            rendered = rendered or {}
            if isinstance(payload, dict):
                match_id = int(payload['match_id'])
                match_dir = (payload.get('match_dir')
                             or rendered.get('match_dir')
                             or _match_dir_from_fixture())
            else:
                match_id = int(payload)
                match_dir = rendered.get('match_dir') or _match_dir_from_fixture()
            body = {
                'match_id': match_id,
                'match_dir': match_dir,
                'season': season,
                'opponent': fixture.get('opponent'),
                'tier': os.environ.get('GUNNERS_REPORT_TIER', 'fallback'),
            }
            url = f'{ANALYST_URL.rstrip("/")}/report'
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode('utf-8'),
                method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                })
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    raw = resp.read().decode('utf-8')
                    data = json.loads(raw) if raw else {}
            except Exception as exc:  # noqa: BLE001
                if optional:
                    print(f'football_analyst_DSPy_agent_match_report soft-skip: {exc}')
                    raise AirflowSkipException(str(exc))
                raise
            path = data.get('report_path') or ''
            print(f'report -> {path}')
            return path

        url = resolve_match_id(await_final_whistle())
        job = scrape(url)
        rendered = visualize(validate(enrich_xg(transform(job))))
        loaded = load_warehouse(rendered)
        football_analyst_DSPy_agent_match_report(loaded, rendered)

    return match_pipeline()


# --- register one DAG per fixture -------------------------------------------
_data = load_fixtures()
_season = _data['season']

for _fixture in _data['fixtures']:
    _d = build_dag(_fixture, _season)
    globals()[_d.dag_id] = _d
