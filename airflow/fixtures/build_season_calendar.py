#!/usr/bin/env python3
"""Build the calendar for a *completed* Arsenal Premier League season.

    python airflow/fixtures/build_season_calendar.py --season 2025/2026

`build_fixtures.py` hand-codes the upcoming season from the published fixture
list, because nothing has been played yet. A finished season is different: the
result is already public, so the calendar is derived rather than typed, and it
carries the two things a backfill needs that the forward-looking file has no
use for -

  sofascore_event_id   the xG source, resolved now so the backfill never has
                       to search for it match by match
  final score          a free end-to-end assertion - if the warehouse disagrees
                       with it after ingest, something aligned the sides wrong

SofaScore's team calendar is the source: one polite JSON call per page of 30,
five pages for a season, no browser. WhoScored has the event stream but a 403
front door, so its match ids are resolved later, through Selenium, by
`orchestrator/backfill_season.py`.

Writes airflow/fixtures/arsenal_<season>.json in the same shape
`db/seed_fixtures.py` already reads, plus the extra keys above.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scraper.sofascore import SofaScoreClient  # noqa: E402

ARSENAL = 'Arsenal'
ARSENAL_SOFASCORE_ID = 42
COMPETITION = 'Premier League'

# WhoScored URL slugs. SofaScore and WhoScored disagree on a handful of club
# names ('Newcastle United' vs 'newcastle'), so the mapping is explicit and the
# builder refuses to guess - an unknown club is a hard error, not a bad slug
# that fails silently three hours into a backfill.
SLUGS = {
    'Arsenal': 'arsenal',
    'Aston Villa': 'aston-villa',
    'Bournemouth': 'bournemouth',
    'AFC Bournemouth': 'bournemouth',
    'Brentford': 'brentford',
    'Brighton & Hove Albion': 'brighton',
    'Brighton and Hove Albion': 'brighton',
    'Burnley': 'burnley',
    'Chelsea': 'chelsea',
    'Coventry City': 'coventry',
    'Crystal Palace': 'crystal-palace',
    'Everton': 'everton',
    'Fulham': 'fulham',
    'Hull City': 'hull',
    'Ipswich Town': 'ipswich',
    'Leeds United': 'leeds',
    'Leicester City': 'leicester',
    'Liverpool': 'liverpool',
    'Liverpool FC': 'liverpool',
    'Luton Town': 'luton',
    'Manchester City': 'manchester-city',
    'Manchester United': 'manchester-united',
    'Newcastle United': 'newcastle',
    'Nottingham Forest': 'nottingham-forest',
    'Sheffield United': 'sheffield-united',
    'Southampton': 'southampton',
    'Sunderland': 'sunderland',
    'Tottenham Hotspur': 'tottenham',
    'West Ham United': 'west-ham',
    'Wolverhampton': 'wolves',
    'Wolverhampton Wanderers': 'wolves',
}


def sofascore_year(season: str) -> str:
    """'2025/2026' -> '25/26', which is how SofaScore labels a season."""
    start, end = season.split('/')
    return f'{start[-2:]}/{end[-2:]}'


def season_slug(season: str) -> str:
    return f"england-premier-league-{season.replace('/', '-')}"


def slug_for(name: str) -> str:
    try:
        return SLUGS[name]
    except KeyError:
        raise SystemExit(
            f'no WhoScored slug for {name!r}. Add it to SLUGS in '
            f'{os.path.relpath(__file__, ROOT)} and re-run.') from None


def fetch_season(season: str, team_id: int = ARSENAL_SOFASCORE_ID,
                 max_pages: int = 12, client: SofaScoreClient | None = None) -> list[dict]:
    """Every finished league match of `season` from the club's calendar."""
    client = client or SofaScoreClient()
    year, out, seen = sofascore_year(season), [], set()

    for page in range(max_pages):
        events = client.team_events(team_id, page=page, past=True)
        if not events:
            break
        for e in events:
            if e.get('season', {}).get('year') != year:
                continue
            tournament = (e.get('tournament', {})
                           .get('uniqueTournament', {}).get('name'))
            if tournament != COMPETITION:
                continue
            if e['id'] in seen:
                continue
            seen.add(e['id'])
            out.append(e)

    out.sort(key=lambda e: e['startTimestamp'])
    return out


def to_fixture(event: dict, season: str, club: str = ARSENAL) -> dict:
    kickoff = datetime.datetime.fromtimestamp(event['startTimestamp'],
                                              datetime.timezone.utc)
    home, away = event['homeTeam']['name'], event['awayTeam']['name']
    home_slug, away_slug = slug_for(home), slug_for(away)
    date = kickoff.strftime('%Y-%m-%d')
    matchweek = (event.get('roundInfo') or {}).get('round')
    finished = (event.get('status') or {}).get('type') == 'finished'

    return {
        'matchweek': matchweek,
        'date': date,
        # UTC, not UK local. A completed season only needs this to order the
        # calendar and to sanity-check the ingest, never to schedule anything.
        'kickoff_uk': kickoff.strftime('%H:%M'),
        'kickoff_confirmed': True,
        'opponent': away if home == club else home,
        'venue': 'H' if home == club else 'A',
        'home_team': home,
        'away_team': away,
        'whoscored_slug': f'{season_slug(season)}-{home_slug}-{away_slug}',
        'fixture_key': f'gw{matchweek:02d}_{date}_{home_slug}_v_{away_slug}',
        # Backfill inputs / assertions.
        'sofascore_event_id': event['id'],
        'final_score': (f"{event['homeScore'].get('current')}-"
                        f"{event['awayScore'].get('current')}") if finished else None,
        'finished': finished,
        # Filled in by the backfill once Selenium has resolved it; pre-fill by
        # hand to pin a match WhoScored's fixture page will not give up.
        'whoscored_match_id': None,
    }


def build(season: str, client: SofaScoreClient | None = None) -> dict:
    events = fetch_season(season, client=client)
    if not events:
        raise SystemExit(f'SofaScore returned no {COMPETITION} matches for {season}')

    fixtures = [to_fixture(e, season) for e in events]
    missing_round = [f['date'] for f in fixtures if f['matchweek'] is None]
    if missing_round:
        raise SystemExit(f'no round number on {len(missing_round)} match(es): {missing_round}')

    return {
        'season': season,
        'competition': COMPETITION,
        'team': ARSENAL,
        'source': [f'https://api.sofascore.com/api/v1/team/{ARSENAL_SOFASCORE_ID}'
                   f'/events/last/{{page}}  (season {sofascore_year(season)})'],
        'notes': 'Completed season, derived not typed. kickoff_uk is UTC. '
                 'sofascore_event_id feeds the xG enrichment; whoscored_match_id is '
                 'resolved by orchestrator/backfill_season.py and may be pre-filled '
                 'by hand for any match the fixtures page will not surface.',
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'fixtures': fixtures,
    }


def default_path(season: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"arsenal_{season.replace('/', '_')}.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--season', default='2025/2026')
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)

    data = build(a.season)
    out = a.out or default_path(a.season)
    with open(out, 'w') as fh:
        json.dump(data, fh, indent=2)

    fixtures = data['fixtures']
    home = sum(1 for f in fixtures if f['venue'] == 'H')
    unfinished = sum(1 for f in fixtures if not f['finished'])
    print(f"wrote {out}")
    print(f"  {len(fixtures)} fixtures ({home} home / {len(fixtures) - home} away), "
          f"matchweeks {min(f['matchweek'] for f in fixtures)}"
          f"..{max(f['matchweek'] for f in fixtures)}")
    print(f"  {len(fixtures) - unfinished} finished, {unfinished} not")
    if len(fixtures) != 38:
        print(f"  ! expected 38 matches in a Premier League season, got {len(fixtures)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
