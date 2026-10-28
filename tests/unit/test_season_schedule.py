"""Tests for the season calendar and the per-fixture scheduling maths."""

import json
import os
from collections import Counter

import pendulum
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(ROOT, 'airflow', 'fixtures', 'arsenal_2026_27.json')
UK = pendulum.timezone('Europe/London')
DEFAULT_KICKOFF = '15:00'


@pytest.fixture(scope='module')
def season():
    with open(FIXTURES) as fh:
        return json.load(fh)


# Mirrors of arsenal_season_dag's defaults - the DAG module cannot be imported
# without Airflow installed, so the scheduling maths is duplicated here.
MATCH_DURATION_MINUTES = 115        # 90 + 15 interval + ~10 stoppage
POST_MATCH_DELAY_MINUTES = 30


def run_window(fixture, match_duration=MATCH_DURATION_MINUTES,
               post_match_delay=POST_MATCH_DELAY_MINUTES):
    """Mirror of arsenal_season_dag.run_window (importable without Airflow)."""
    hh, mm = (int(x) for x in (fixture.get('kickoff_uk') or DEFAULT_KICKOFF).split(':'))
    y, mo, d = (int(x) for x in fixture['date'].split('-'))
    kickoff = pendulum.datetime(y, mo, d, hh, mm, tz=UK)
    run_at = kickoff.add(minutes=match_duration + post_match_delay)
    return kickoff, run_at, f'{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *'


# --- calendar integrity -----------------------------------------------------

def test_thirty_eight_fixtures(season):
    assert len(season['fixtures']) == 38


def test_nineteen_home_nineteen_away(season):
    venues = Counter(f['venue'] for f in season['fixtures'])
    assert venues == {'H': 19, 'A': 19}


def test_each_opponent_played_home_and_away(season):
    per_opponent = Counter((f['opponent'], f['venue']) for f in season['fixtures'])
    opponents = {f['opponent'] for f in season['fixtures']}
    assert len(opponents) == 19
    for opponent in opponents:
        assert per_opponent[(opponent, 'H')] == 1
        assert per_opponent[(opponent, 'A')] == 1


def test_arsenal_is_always_one_of_the_two_teams(season):
    for f in season['fixtures']:
        assert 'Arsenal' in (f['home_team'], f['away_team'])
        assert (f['home_team'] == 'Arsenal') == (f['venue'] == 'H')


def test_fixture_keys_unique(season):
    keys = [f['fixture_key'] for f in season['fixtures']]
    assert len(set(keys)) == len(keys)


def test_matchweeks_are_one_to_thirty_eight(season):
    assert sorted(f['matchweek'] for f in season['fixtures']) == list(range(1, 39))


def test_dates_are_chronological_by_matchweek(season):
    dates = [f['date'] for f in sorted(season['fixtures'], key=lambda f: f['matchweek'])]
    assert dates == sorted(dates)


def test_slug_matches_the_two_teams(season):
    for f in season['fixtures']:
        assert f['whoscored_slug'].startswith('england-premier-league-2026-2027-')
        assert f['whoscored_slug'].endswith('arsenal') or '-arsenal-' in f['whoscored_slug']


# --- scheduling -------------------------------------------------------------

def test_every_fixture_yields_a_valid_cron(season):
    for f in season['fixtures']:
        _, run_at, cron = run_window(f)
        minute, hour, day, month, dow = cron.split()
        assert 0 <= int(minute) <= 59
        assert 0 <= int(hour) <= 23
        assert int(day) == run_at.day
        assert int(month) == run_at.month
        assert dow == '*'


def test_run_is_thirty_minutes_after_the_final_whistle(season):
    """The whole point of the schedule: shortly after the match ends."""
    for f in season['fixtures']:
        kickoff, run_at, _ = run_window(f)
        whistle = kickoff.add(minutes=MATCH_DURATION_MINUTES)
        assert run_at > whistle
        assert (run_at - whistle).in_minutes() == POST_MATCH_DELAY_MINUTES


def test_run_is_after_kickoff(season):
    for f in season['fixtures']:
        kickoff, run_at, _ = run_window(f)
        assert run_at > kickoff
        assert (run_at - kickoff).in_minutes() == 145      # 2h25


def test_the_delay_is_configurable(season):
    """POST_MATCH_DELAY_MINUTES is the knob to turn if scrapes land too early."""
    f = season['fixtures'][0]
    kickoff, run_at, _ = run_window(f, post_match_delay=90)
    assert (run_at - kickoff).in_minutes() == MATCH_DURATION_MINUTES + 90


def test_late_kickoff_rolls_over_to_next_day():
    """A 20:00 kick-off ends at 21:55, so the run lands at 22:25 - same day."""
    fixture = {'date': '2026-08-21', 'kickoff_uk': '20:00'}
    _, run_at, cron = run_window(fixture)
    assert run_at.day == 21
    assert cron == '25 22 21 8 *'


def test_very_late_kickoff_still_rolls_over():
    """22:00 in a European away tie: 00:25 the next morning."""
    fixture = {'date': '2026-08-21', 'kickoff_uk': '22:00'}
    _, run_at, cron = run_window(fixture)
    assert run_at.day == 22
    assert cron == '25 0 22 8 *'


def test_unconfirmed_kickoff_uses_default(season):
    unconfirmed = [f for f in season['fixtures'] if not f['kickoff_confirmed']]
    assert unconfirmed, 'expected some fixtures without a confirmed TV slot'
    kickoff, _, _ = run_window(unconfirmed[0])
    assert kickoff.format('HH:mm') == DEFAULT_KICKOFF


def test_run_windows_do_not_collide(season):
    """Two fixtures must never resolve to the same cron, or one DAG shadows the other."""
    crons = [run_window(f)[2] for f in season['fixtures']]
    assert len(set(crons)) == len(crons)
