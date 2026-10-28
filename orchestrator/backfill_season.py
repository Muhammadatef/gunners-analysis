#!/usr/bin/env python3
"""Ingest a completed season, one match at a time, resumably.

    python orchestrator/backfill_season.py --season 2025/2026

`airflow/fixtures/build_season_calendar.py` writes the calendar this reads: 38
fixtures, each already carrying the two ids that would otherwise cost a page
load apiece -

    whoscored_match_id   the event stream, resolved from Arsenal's fixtures
                         page (one Selenium fetch for the whole season)
    sofascore_event_id   the xG, resolved from the club calendar JSON

so the only browser work left here is the thing that genuinely needs a browser:
fetching each match page. That is still the slow, fragile part - a real Chrome
per match against a bot-protected site - which is what the rest of this file is
shaped around.

Three properties matter more than speed:

**Resumable.** `matches.match_id` *is* the WhoScored match id, so the set of
finished work is a single query, not a bookkeeping file that can drift from
reality. Re-running after a crash, a bot wall, or a laptop lid closing picks up
exactly where it stopped. The ledger under `data/backfill/` is written for the
operator's benefit, never read back as truth.

**Non-fatal per match.** One blocked page must not cost the other 37. A failure
is recorded and the run moves on; the exit code and the summary say what needs
another pass. Run it again and it retries only what is missing.

**Self-checking.** The calendar carries each match's real final score, taken
from a source the pipeline never touches. After a match loads, the warehouse is
asked what it thinks the score was. Home and away are positional all the way
through this pipeline, so a transposed fixture produces plausible, entirely
wrong data - and comparing against an independent score is what catches it.

    --dry-run          print the plan, touch nothing
    --only 1,2,5-9     restrict to those matchweeks
    --force            re-ingest matches already in the warehouse
    --pause 20         seconds between matches
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
import uuid

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.connection import DEFAULT_SCHEMA, club_schema, cursor, ensure_schema, use_schema  # noqa: E402
from orchestrator import pipeline                                                          # noqa: E402

logger = logging.getLogger('backfill')

LEDGER_DIR = os.path.join(ROOT, 'data', 'backfill')

# resolve_event_id() short-circuits on this fragment, so a pre-resolved event id
# costs no HTTP at all. The slug is decorative - only the id after '#id:' is read.
SOFASCORE_URL = 'https://www.sofascore.com/football/match/x/x#id:{event_id}'
WHOSCORED_URL = 'https://www.whoscored.com/matches/{match_id}/live/{slug}'


def calendar_path(season: str) -> str:
    return os.path.join(ROOT, 'airflow', 'fixtures',
                        f"arsenal_{season.replace('/', '_')}.json")


def load_calendar(season: str, path: str | None = None) -> dict:
    """The season calendar, refusing to start a long run on a broken one."""
    path = path or calendar_path(season)
    if not os.path.exists(path):
        raise SystemExit(
            f'no calendar at {path}\n'
            f'build it first:  python airflow/fixtures/build_season_calendar.py '
            f'--season {season}')

    with open(path) as fh:
        data = json.load(fh)

    missing_ws = [f['fixture_key'] for f in data['fixtures']
                  if not f.get('whoscored_match_id')]
    missing_ss = [f['fixture_key'] for f in data['fixtures']
                  if not f.get('sofascore_event_id')]
    if missing_ws:
        raise SystemExit(
            f'{len(missing_ws)} fixture(s) have no whoscored_match_id, starting with '
            f'{missing_ws[0]}.\nResolve them first - see resolve_ids() - or pre-fill '
            f'the field by hand in {os.path.relpath(path, ROOT)}.')
    if missing_ss:
        raise SystemExit(f'{len(missing_ss)} fixture(s) have no sofascore_event_id, '
                         f'starting with {missing_ss[0]}. Rebuild the calendar.')
    return data


def resolve_ids(season: str, path: str | None = None, team_id: int = 13) -> int:
    """Fill in every missing `whoscored_match_id` from team + PL fixture pages.

    Arsenal's WhoScored fixtures page lists the completed season alongside the
    current one; the PL tournament fixtures page fills gaps when the team page
    is incomplete. One browser session, two page loads max.
    """
    from scraper.fixtures import TEAM_FIXTURES_URL, pl_fixtures_url, resolve_from_html
    from scraper.scraper import WhoscoredScraper

    path = path or calendar_path(season)
    with open(path) as fh:
        data = json.load(fh)

    todo = [f for f in data['fixtures'] if not f.get('whoscored_match_id')]
    if not todo:
        logger.info('every fixture already has a whoscored_match_id')
        return 0

    scraper = WhoscoredScraper()
    try:
        pages = [
            scraper.fetch_html(TEAM_FIXTURES_URL.format(team_id=team_id), wait_time=12),
            scraper.fetch_html(pl_fixtures_url(), wait_time=12),
        ]
    finally:
        scraper.close()

    found = 0
    for fixture in todo:
        match_id = None
        for html in pages:
            match_id = resolve_from_html(html, fixture['whoscored_slug'])
            if match_id:
                break
        if match_id:
            fixture['whoscored_match_id'] = match_id
            found += 1
        else:
            logger.warning('no match id for %s', fixture['whoscored_slug'])

    with open(path, 'w') as fh:
        json.dump(data, fh, indent=2)
    logger.info('resolved %d/%d match ids -> %s', found, len(todo), path)
    return found


def already_loaded(schema: str) -> set[int]:
    """WhoScored match ids already in the warehouse - the resume set."""
    with use_schema(schema), cursor() as cur:
        cur.execute('SELECT match_id FROM matches')
        return {row[0] for row in cur.fetchall()}


def stored_match(schema: str, match_id: int) -> dict | None:
    """What the warehouse ended up believing about a match.

    Read back rather than trusted from the pipeline's return value: the point
    of the check is that the row on disk is right, not that the code thought so.
    """
    with use_schema(schema), cursor() as cur:
        cur.execute("""SELECT home_score, away_score, home_xg, away_xg,
                              home_xgot, away_xgot, xg_source
                       FROM matches WHERE match_id = %s""", (match_id,))
        row = cur.fetchone()
    if not row:
        return None

    def pair(left, right):
        # Both halves or nothing: one side missing is as broken as both, and a
        # side with no shots on target legitimately has 0.0, which must survive.
        return None if left is None or right is None else f'{left}-{right}'

    return {
        'score': f'{row[0]}-{row[1]}',
        'xg': pair(row[2], row[3]),
        'xgot': pair(row[4], row[5]),
        'xg_source': row[6],
    }


def xg_complete(stored: dict) -> bool:
    """Both xG and xGOT present, and sourced from SofaScore or FotMob.

    WhoScored's feed carries neither, so anything else means the enrichment
    quietly fell through and the shot map and GK panels are placeholders.
    """
    src = (stored.get('xg_source') or '').lower()
    return bool(stored['xg'] and stored['xgot']
                and any(s in src for s in ('sofascore', 'fotmob'))
                and 'unavailable' not in src)


def parse_only(spec: str | None) -> set[int] | None:
    """'1,2,5-9' -> {1,2,5,6,7,8,9}."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            lo, hi = part.split('-', 1)
            out.update(range(int(lo), int(hi) + 1))
        elif part:
            out.add(int(part))
    return out


def select(calendar: dict, done: set[int], only: set[int] | None,
           force: bool) -> tuple[list[dict], list[dict]]:
    """Split the calendar into (to run, skipped)."""
    todo, skipped = [], []
    for fixture in calendar['fixtures']:
        if only is not None and fixture['matchweek'] not in only:
            continue
        if not fixture.get('finished'):
            skipped.append({**fixture, 'why': 'not finished'})
            continue
        if fixture['whoscored_match_id'] in done and not force:
            skipped.append({**fixture, 'why': 'already in the warehouse'})
            continue
        todo.append(fixture)
    return todo, skipped


def run_one(fixture: dict, season: str, competition: str, schema: str,
            *, wait: int, dpi: int, min_events: int | None) -> dict:
    """Scrape, enrich, render and load one match. Raises on failure."""
    job_id = str(uuid.uuid4())
    whoscored = WHOSCORED_URL.format(match_id=fixture['whoscored_match_id'],
                                     slug=fixture['whoscored_slug'])
    sofascore = SOFASCORE_URL.format(event_id=fixture['sofascore_event_id'])

    with use_schema(schema):
        result = pipeline.run_match(
            job_id, whoscored, season,
            fixture_key=fixture['fixture_key'],
            sofascore_url=sofascore,
            club=schema,
            competition=competition,
            matchweek=fixture['matchweek'],
            load_to_warehouse=True,
            min_events=min_events,
            dpi=dpi,
            wait_time=wait,
        )
    result['job_id'] = job_id
    return result


def write_ledger(season: str, entries: list[dict], started: str) -> str:
    os.makedirs(LEDGER_DIR, exist_ok=True)
    path = os.path.join(LEDGER_DIR, f"{season.replace('/', '_')}.json")
    done = [e for e in entries if e['status'] == 'ok']
    with open(path, 'w') as fh:
        json.dump({
            'season': season,
            'started_at': started,
            'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'ok': len(done),
            'failed': sum(1 for e in entries if e['status'] == 'failed'),
            'matches': entries,
        }, fh, indent=2)
    return path


def backfill(season: str, *, club: str = DEFAULT_SCHEMA, only: set[int] | None = None,
             force: bool = False, pause: float = 20.0, wait: int = 15, dpi: int = 150,
             min_events: int | None = None, dry_run: bool = False,
             seed: bool = True, calendar_file: str | None = None) -> int:
    calendar = load_calendar(season, calendar_file)
    competition = calendar.get('competition', 'Premier League')
    schema = club_schema(club)

    if not dry_run:
        ensure_schema(schema)
        if seed:
            from db.seed_fixtures import seed as seed_fixtures
            seed_fixtures(calendar_file or calendar_path(season))

    # The resume query is read-only, so a dry run still reports honestly on what
    # it would skip rather than pretending the warehouse is empty.
    from db.connection import schema_exists
    done = already_loaded(schema) if schema_exists(schema) else set()
    todo, skipped = select(calendar, done, only, force)

    print(f'season      {season}  ({competition})')
    print(f'schema      {schema}')
    print(f'calendar    {len(calendar["fixtures"])} fixtures')
    print(f'to ingest   {len(todo)}')
    print(f'skipping    {len(skipped)}'
          + (f'  ({skipped[0]["why"]}, ...)' if skipped else ''))
    if dry_run:
        for f in todo:
            print(f'  gw{f["matchweek"]:02d}  {f["date"]}  '
                  f'{f["home_team"]} v {f["away_team"]}  '
                  f'ws={f["whoscored_match_id"]} ss={f["sofascore_event_id"]}')
        return 0
    if not todo:
        print('\nnothing to do - the season is already complete.')
        return 0

    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entries: list[dict] = []
    t0 = time.time()

    for n, fixture in enumerate(todo, 1):
        label = (f'gw{fixture["matchweek"]:02d}  {fixture["date"]}  '
                 f'{fixture["home_team"]} v {fixture["away_team"]}')
        print(f'\n[{n}/{len(todo)}] {label}', flush=True)
        entry = {
            'matchweek': fixture['matchweek'],
            'date': fixture['date'],
            'fixture_key': fixture['fixture_key'],
            'whoscored_match_id': fixture['whoscored_match_id'],
            'expected_score': fixture['final_score'],
            'status': 'running',
        }
        start = time.time()
        try:
            result = run_one(fixture, season, competition, schema,
                             wait=wait, dpi=dpi, min_events=min_events)
        except Exception as exc:                                     # noqa: BLE001
            entry.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                         seconds=round(time.time() - start, 1))
            print(f'   FAILED  {type(exc).__name__}: {str(exc).splitlines()[0][:160]}')
        else:
            entry.update(status='ok', seconds=round(time.time() - start, 1),
                         job_id=result['job_id'], match_id=result.get('match_id'),
                         source=result.get('source'), xg_source=result.get('xg_source'),
                         match_dir=result.get('match_dir'))

            # Two independent checks against the row that actually landed: does
            # the score agree with a source the pipeline never touched, and did
            # both xG and xGOT make it in.
            stored = stored_match(schema, result['match_id']) if result.get('match_id') else None
            entry['stored'] = stored
            where = os.path.basename(result.get('match_dir') or '')

            if stored and stored['score'] != fixture['final_score']:
                entry['status'] = 'score-mismatch'
                print(f'   ! score mismatch: warehouse {stored["score"]}, '
                      f'expected {fixture["final_score"]} - sides may be transposed')
            elif stored and not xg_complete(stored):
                entry['status'] = 'no-xg'
                print(f'   ! {stored["score"]} loaded, but xG/xGOT are incomplete '
                      f'(xg={stored["xg"]} xgot={stored["xgot"]} '
                      f'source={stored["xg_source"]}) -> {where}')
            else:
                print(f'   ok  {stored["score"]}  xg={stored["xg"]}  '
                      f'xgot={stored["xgot"]}  {entry["seconds"]}s  -> {where}')

        entries.append(entry)
        write_ledger(season, entries, started)

        if n < len(todo) and pause:
            time.sleep(pause)

    ok = sum(1 for e in entries if e['status'] == 'ok')
    bad = [e for e in entries if e['status'] == 'failed']
    odd = [e for e in entries if e['status'] == 'score-mismatch']
    noxg = [e for e in entries if e['status'] == 'no-xg']

    def weeks(rows):
        return ', '.join(f'gw{e["matchweek"]}' for e in rows)

    print('\n' + '-' * 62)
    print(f'ingested   {ok}/{len(todo)} in {round((time.time() - t0) / 60, 1)} min')
    if odd:
        print(f'mismatched {len(odd)}: {weeks(odd)}')
    if noxg:
        print(f'no xG/xGOT {len(noxg)}: {weeks(noxg)}')
        print('           the events loaded; re-run with --force to retry enrichment.')
    if bad:
        print(f'failed     {len(bad)}: {weeks(bad)}')
        print('re-run the same command to retry only these.')
    print(f'ledger     {write_ledger(season, entries, started)}')
    return 1 if (bad or odd or noxg) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--season', default='2025/2026')
    ap.add_argument('--club', default=DEFAULT_SCHEMA)
    ap.add_argument('--calendar', default=None, help='override the calendar path')
    ap.add_argument('--only', default=None, help="matchweeks, e.g. '1,2,5-9'")
    ap.add_argument('--force', action='store_true',
                    help='re-ingest matches already in the warehouse')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-seed', action='store_true',
                    help='skip seeding the calendar into the warehouse')
    ap.add_argument('--pause', type=float, default=20.0,
                    help='seconds between matches (default 20)')
    ap.add_argument('--wait', type=int, default=15)
    ap.add_argument('--dpi', type=int, default=150)
    ap.add_argument('--min-events', type=int, default=None)
    ap.add_argument('--mode', default=None,
                    help='browser mode: headed, xvfb or headless')
    ap.add_argument('--resolve-ids', action='store_true',
                    help='only fill in missing whoscored_match_ids, then exit')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s  %(levelname)-7s %(name)s: %(message)s',
                        datefmt='%H:%M:%S')
    # Quieten the per-request chatter from the two clients; the per-match lines
    # this file prints are the useful signal over a two-hour run.
    for noisy in ('selenium', 'urllib3', 'WDM', 'scraper.sofascore'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if a.mode:
        os.environ['GUNNERS_BROWSER_MODE'] = a.mode

    if a.resolve_ids:
        resolve_ids(a.season, a.calendar)
        return 0

    return backfill(a.season, club=a.club, only=parse_only(a.only), force=a.force,
                    pause=a.pause, wait=a.wait, dpi=a.dpi, min_events=a.min_events,
                    dry_run=a.dry_run, seed=not a.no_seed, calendar_file=a.calendar)


if __name__ == '__main__':
    sys.exit(main())
