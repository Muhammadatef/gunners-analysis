"""Tests for the data quality gate that guards every automated ingest."""

import numpy as np
import pandas as pd
import pytest

from quality.checks import ERROR, run_checks


def _events(n=1400, max_minute=95):
    """A synthetic but structurally valid match."""
    rng = np.random.default_rng(7)
    minutes = np.linspace(0, max_minute, n).astype(int)
    types = np.where(np.arange(n) % 2 == 0, 'Pass', 'BallRecovery').astype(object)
    types[5] = 'Goal'
    types[6] = 'Goal'
    types[7] = 'Goal'
    return pd.DataFrame({
        'minute': minutes,
        'period': np.where(minutes < 46, 'FirstHalf', 'SecondHalf'),
        'teamId': np.where(np.arange(n) % 2 == 0, 13, 162),
        'playerId': rng.choice([1, 2, 3, 4], n),
        'type': types,
        'outcomeType': 'Successful',
        'x': rng.uniform(0, 105, n),
        'y': rng.uniform(0, 68, n),
        'endX': rng.uniform(0, 105, n),
        'endY': rng.uniform(0, 68, n),
        'xT': rng.uniform(-0.02, 0.02, n),
        'pro': np.concatenate([np.full(80, 12.0), np.zeros(n - 80)]),
    })


def _players():
    rows = []
    for team in (13, 162):
        for i in range(11):
            rows.append({'playerId': i + 1, 'teamId': team, 'isFirstEleven': True})
        for i in range(9):
            rows.append({'playerId': i + 1, 'teamId': team, 'isFirstEleven': False})
    return pd.DataFrame(rows)


def _raw(score='1 : 2'):
    return {'matchCentreData': {'ftScore': score}}


def _named(report, name):
    return next(c for c in report['checks'] if c['name'] == name)


def test_healthy_match_passes():
    report = run_checks(_events(), _players(), _raw())
    assert report['passed'] is True
    assert report['status'] == 'PASS'
    assert report['checks_failed'] == 0


def test_truncated_scrape_is_an_error():
    report = run_checks(_events(n=300, max_minute=30), _players(), _raw())
    assert report['passed'] is False
    assert _named(report, 'event_count_in_range')['passed'] is False
    assert _named(report, 'covers_full_match')['passed'] is False


def test_first_half_only_is_an_error():
    events = _events()
    events['period'] = 'FirstHalf'
    report = run_checks(events, _players(), _raw())
    assert _named(report, 'both_halves_present')['passed'] is False
    assert report['passed'] is False


def test_goal_events_must_match_the_scoreline():
    report = run_checks(_events(), _players(), _raw('3 : 2'))
    check = _named(report, 'goal_events_match_scoreline')
    assert check['passed'] is False
    assert check['severity'] == ERROR
    assert report['passed'] is False


def test_out_of_bounds_coordinates_are_an_error():
    events = _events()
    events.loc[0, 'x'] = 400.0
    report = run_checks(events, _players(), _raw())
    assert _named(report, 'coordinates_within_pitch')['passed'] is False


def test_missing_starters_are_an_error():
    players = _players()
    players.loc[0, 'isFirstEleven'] = False
    report = run_checks(_events(), players, _raw())
    assert _named(report, 'eleven_starters_per_team')['passed'] is False


def test_event_player_not_in_squad_is_an_error():
    events = _events()
    events.loc[0, 'playerId'] = 9999
    report = run_checks(events, _players(), _raw())
    assert _named(report, 'no_orphan_player_ids')['passed'] is False


def test_three_teams_is_an_error():
    events = _events()
    events.loc[0, 'teamId'] = 99
    report = run_checks(events, _players(), _raw())
    assert _named(report, 'exactly_two_teams')['passed'] is False


def test_warnings_do_not_block_the_pipeline():
    """Thin xT coverage is worth flagging but must not fail the ingest."""
    events = _events()
    events['xT'] = np.nan
    report = run_checks(events, _players(), _raw())
    assert _named(report, 'xt_coverage')['passed'] is False
    assert report['warnings'] >= 1
    assert report['passed'] is True


def test_unparseable_scoreline_is_skipped_not_failed():
    report = run_checks(_events(), _players(), {'matchCentreData': {'ftScore': 'abandoned'}})
    assert _named(report, 'goal_events_match_scoreline')['passed'] is True
    assert report['passed'] is True
