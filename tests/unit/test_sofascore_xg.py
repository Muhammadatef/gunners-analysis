"""Extracting team xG/xGOT from a SofaScore bundle.

The shot map is the primary source; /statistics is the fallback for leagues
SofaScore does not model shot by shot. Neither has to exist.
"""

import pytest

from scraper.sofascore import bundle_teams, names_match, xg_from_bundle


def _shots(*rows):
    return {'shotmap': [{'isHome': h, 'xg': xg, 'xgot': xgot} for h, xg, xgot in rows]}


def _stats(home, away, name='Expected goals'):
    return {'statistics': [{'period': 'ALL', 'groups': [
        {'statisticsItems': [{'name': name, 'home': home, 'away': away}]}]}]}


# --- shot map ---------------------------------------------------------------

def test_sums_shots_by_side():
    out = xg_from_bundle({'shotmap': _shots(
        (True, 0.35, 0.20), (True, 0.11, None), (False, 0.42, 0.51))})
    assert out == {'home_xg': 0.46, 'away_xg': 0.42,
                   'home_xgot': 0.2, 'away_xgot': 0.51}


def test_rounds_to_two_places():
    out = xg_from_bundle({'shotmap': _shots(
        (True, 0.333, None), (True, 0.333, None), (True, 0.333, None))})
    assert out['home_xg'] == 1.0


def test_a_side_with_no_shots_is_zero_not_none():
    out = xg_from_bundle({'shotmap': _shots((True, 0.5, 0.4))})
    assert out['home_xg'] == 0.5
    assert out['away_xg'] == 0.0


# --- statistics fallback ----------------------------------------------------

def test_falls_back_to_team_statistics():
    out = xg_from_bundle({'shotmap': {'shotmap': []}, 'statistics': _stats('1.84', '0.97')})
    assert out['home_xg'] == 1.84
    assert out['away_xg'] == 0.97
    # SofaScore publishes no team-level xGOT, so it stays unknown rather than 0.
    assert out['home_xgot'] is None


def test_statistics_are_not_consulted_when_the_shot_map_has_xg():
    out = xg_from_bundle({'shotmap': _shots((True, 0.5, None)),
                          'statistics': _stats('9.99', '9.99')})
    assert out['home_xg'] == 0.5


def test_other_statistics_items_are_ignored():
    out = xg_from_bundle({'shotmap': {'shotmap': []},
                          'statistics': _stats('61%', '39%', name='Ball possession')})
    assert out['home_xg'] is None


def test_only_the_full_match_period_counts():
    stats = {'statistics': [{'period': '1ST', 'groups': [
        {'statisticsItems': [{'name': 'Expected goals', 'home': '0.5', 'away': '0.2'}]}]}]}
    assert xg_from_bundle({'shotmap': {'shotmap': []}, 'statistics': stats})['home_xg'] is None


# --- nothing available ------------------------------------------------------

@pytest.mark.parametrize('bundle', [
    {},
    {'shotmap': None, 'statistics': None},
    {'shotmap': {'shotmap': []}, 'statistics': {'statistics': []}},
    {'shotmap': {'shotmap': [{'isHome': True, 'goal': True}]}},     # shots but no xg
])
def test_absent_xg_is_none_throughout(bundle):
    assert xg_from_bundle(bundle) == {
        'home_xg': None, 'away_xg': None, 'home_xgot': None, 'away_xgot': None}


# --- team names -------------------------------------------------------------

def test_bundle_teams():
    assert bundle_teams({'event': {'homeTeam': {'name': 'Barcelona'},
                                   'awayTeam': {'name': 'Inter'}}}) == ('Barcelona', 'Inter')


def test_bundle_teams_on_an_empty_bundle():
    assert bundle_teams({}) == (None, None)


@pytest.mark.parametrize('a,b,expected', [
    ('Brighton & Hove Albion', 'Brighton', True),
    ('Barcelona', 'FC Barcelona', True),
    ('Arsenal', 'Chelsea', False),
    ('Al Ahly', 'Al Ahly SC', True),
])
def test_names_match(a, b, expected):
    assert names_match(a, b) is expected


# WhoScored abbreviates where SofaScore spells out, and the two feeds have to
# be recognised as the same club or the match silently loses its xG.
@pytest.mark.parametrize('whoscored,sofascore', [
    ('Man Utd', 'Manchester United'),
    ('Man City', 'Manchester City'),
    ('Spurs', 'Tottenham Hotspur'),
    ('Wolves', 'Wolverhampton'),
    ("Nott'm Forest", 'Nottingham Forest'),
    ('Nott’m Forest', 'Nottingham Forest'),          # typographic apostrophe
    ('Newcastle', 'Newcastle United'),
    ('Leeds', 'Leeds United'),
    ('West Ham', 'West Ham United'),
    ('Liverpool', 'Liverpool FC'),
])
def test_abbreviations_match_their_full_names(whoscored, sofascore):
    assert names_match(whoscored, sofascore) is True


# The more dangerous direction. Home and away are positional all the way
# through the pipeline, so a false positive writes one club's xG onto another
# club's match without a word. 'united' and 'city' are stripped as noise, which
# used to leave both Manchester clubs as {'manchester'} - matching each other.
@pytest.mark.parametrize('a,b', [
    ('Man Utd', 'Manchester City'),
    ('Man City', 'Manchester United'),
    ('Manchester United', 'Manchester City'),
    ('Sheffield United', 'Newcastle United'),
    ('Leicester City', 'Manchester City'),
])
def test_different_clubs_never_match(a, b):
    assert names_match(a, b) is False


def test_names_match_tolerates_none():
    assert names_match('Arsenal', None) is False
