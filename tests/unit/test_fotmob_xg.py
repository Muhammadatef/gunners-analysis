"""Unit tests for FotMob xG extraction (no network)."""

from scraper.fotmob import xg_from_details, teams_from_details


DETAILS = {
    'general': {
        'homeTeam': {'id': 1, 'name': 'Arsenal'},
        'awayTeam': {'id': 2, 'name': 'Coventry'},
    },
    'header': {
        'teams': [{'name': 'Arsenal'}, {'name': 'Coventry'}],
    },
    'content': {
        'stats': {
            'Periods': {
                'All': {
                    'stats': [
                        {
                            'title': 'Top stats',
                            'stats': [
                                {
                                    'title': 'Expected goals (xG)',
                                    'stats': ['1.88', '0.20'],
                                },
                            ],
                        },
                        {
                            'title': 'Expected goals (xG)',
                            'stats': [
                                {
                                    'title': 'xG on target (xGOT)',
                                    'stats': ['2.44', '0.04'],
                                },
                            ],
                        },
                    ],
                },
            },
        },
        'shotmap': {'shots': []},
    },
}


def test_xg_from_details_reads_summary_rows():
    xg = xg_from_details(DETAILS)
    assert xg == {
        'home_xg': 1.88,
        'away_xg': 0.20,
        'home_xgot': 2.44,
        'away_xgot': 0.04,
    }


def test_teams_from_details():
    assert teams_from_details(DETAILS) == ('Arsenal', 'Coventry')


def test_shotmap_fallback_when_stats_missing():
    details = {
        'general': {
            'homeTeam': {'id': 10, 'name': 'A'},
            'awayTeam': {'id': 20, 'name': 'B'},
        },
        'content': {
            'stats': {'Periods': {'All': {'stats': []}}},
            'shotmap': {
                'shots': [
                    {'teamId': 10, 'expectedGoals': 0.4, 'expectedGoalsOnTarget': 0.5},
                    {'teamId': 20, 'expectedGoals': 0.1, 'expectedGoalsOnTarget': None},
                ],
            },
        },
    }
    xg = xg_from_details(details)
    assert xg['home_xg'] == 0.4
    assert xg['away_xg'] == 0.1
    assert xg['home_xgot'] == 0.5
    assert xg['away_xgot'] is None


# --- match_status: waiting for a whistle the calendar cannot predict ---------

def _entry(mid, league, home, away, *, finished=False, started=False,
           utc='2026-09-12T19:00:00.000Z', score=None):
    return {
        'id': mid,
        '_league': league,
        'home': {'name': home},
        'away': {'name': away},
        'status': {
            'utcTime': utc, 'finished': finished, 'started': started,
            'cancelled': False, 'scoreStr': score,
            'reason': {'long': 'Full-Time'} if finished else {},
        },
    }


# 12 Sep 2026 really did list both of these. Only one is the senior fixture.
LISTING = [
    _entry(5795453, 'Premier League', 'Sunderland', 'Arsenal',
           finished=True, started=True, score='0 - 2'),
    _entry(1000019121, 'Premier League 2', 'Arsenal U21', 'Chelsea U21',
           finished=True, started=True, score='1 - 4'),
]


def _client(listing=LISTING):
    from scraper.fotmob import FotMobClient
    client = FotMobClient.__new__(FotMobClient)
    client.matches_on = lambda date: listing
    return client


def test_finished_match_reports_real_kickoff_and_score():
    status = _client().match_status('2026-09-12', 'Sunderland', 'Arsenal',
                                    league='Premier League')
    assert status['finished'] is True
    assert status['score'] == '0 - 2'
    assert status['fotmob_id'] == 5795453
    # 19:00Z is 20:00 UK - the kick-off the calendar guessed as 15:00.
    assert status['utc_kickoff'] == '2026-09-12T19:00:00.000Z'


def test_unfinished_match_is_reported_as_such():
    listing = [_entry(1, 'Premier League', 'Brighton', 'Arsenal')]
    status = _client(listing).match_status('2026-09-19', 'Brighton', 'Arsenal')
    assert status['finished'] is False
    assert status['started'] is False


def test_fixture_not_listed_returns_none():
    assert _client().match_status('2026-09-12', 'Arsenal', 'Liverpool') is None


def test_age_group_side_is_not_mistaken_for_the_senior_one():
    """names_match is token overlap, so 'Arsenal' also matches 'Arsenal U21'."""
    assert _client().match_status('2026-09-12', 'Arsenal', 'Chelsea') is None


def test_asking_for_the_age_group_side_still_finds_it():
    status = _client().match_status('2026-09-12', 'Arsenal U21', 'Chelsea U21')
    assert status['fotmob_id'] == 1000019121


def test_find_match_skips_the_youth_fixture_too():
    """enrich_xg goes through find_match; it must not pick up academy xG."""
    assert _client().find_match('2026-09-12', 'Sunderland', 'Arsenal') == 5795453
    assert _client().find_match('2026-09-12', 'Arsenal', 'Chelsea') is None
