"""The quality gate's competition-sensitive numbers.

MIN_EVENTS is calibrated to a Premier League match (~1200-2000 Opta events). A
sparser feed - lower tiers, some non-European leagues - trips it on a perfectly
complete match, so it has to be relaxable per run without loosening the default.
"""

import numpy as np
import pandas as pd
import pytest

from quality.checks import MIN_EVENTS, Thresholds, run_checks


def _events(n=1400, max_minute=95):
    """A synthetic but internally consistent event stream for two teams."""
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        'minute': np.linspace(0, max_minute, n).astype(int),
        'period': ['FirstHalf'] * (n // 2) + ['SecondHalf'] * (n - n // 2),
        'teamId': [13, 31] * (n // 2),
        'playerId': rng.choice([1, 2, 3, 4], n),
        'x': rng.uniform(0, 105, n),
        'y': rng.uniform(0, 68, n),
        'endX': rng.uniform(0, 105, n),
        'type': ['Pass'] * n,
        'xT': rng.uniform(0, 0.1, n),
        'pro': rng.uniform(0, 20, n),
    })


def _players():
    rows = []
    for team in (13, 31):
        for i in range(16):
            rows.append({'playerId': (1 + i) if team == 13 else (1 + i),
                         'teamId': team, 'isFirstEleven': i < 11})
    return pd.DataFrame(rows)


def _named(report, name):
    return next(c for c in report['checks'] if c['name'] == name)


def test_defaults_reject_a_sparse_feed():
    report = run_checks(_events(n=500), _players())
    assert _named(report, 'event_count_in_range')['passed'] is False
    assert report['passed'] is False


def test_a_relaxed_minimum_accepts_it():
    report = run_checks(_events(n=500), _players(), thresholds=Thresholds(min_events=400))
    assert _named(report, 'event_count_in_range')['passed'] is True


def test_relaxing_one_threshold_keeps_the_others():
    t = Thresholds(min_events=400)
    assert t.max_events == 3000
    assert t.min_full_time_minute == 90


def test_thresholds_are_recorded_in_the_report():
    """quality_reports.report is stored verbatim, so it should be self-describing."""
    report = run_checks(_events(), _players(), thresholds=Thresholds(min_events=400))
    assert report['thresholds'] == {'min_events': 400, 'max_events': 3000,
                                    'min_full_time_minute': 90}


def test_defaults_are_reported_when_nothing_is_overridden():
    report = run_checks(_events(), _players())
    assert report['thresholds']['min_events'] == MIN_EVENTS


def test_extra_time_still_passes_the_defaults():
    """A 120-minute cup tie must not look like a truncated scrape."""
    report = run_checks(_events(n=2400, max_minute=122), _players())
    assert _named(report, 'event_count_in_range')['passed'] is True
    assert _named(report, 'covers_full_match')['passed'] is True


def test_a_truncated_scrape_is_still_caught_when_relaxed():
    """Relaxing the floor must not disable the check."""
    report = run_checks(_events(n=120), _players(), thresholds=Thresholds(min_events=400))
    assert _named(report, 'event_count_in_range')['passed'] is False


@pytest.mark.parametrize('thresholds', [None, Thresholds()])
def test_none_and_defaults_agree(thresholds):
    report = run_checks(_events(), _players(), thresholds=thresholds)
    assert report['thresholds']['min_events'] == MIN_EVENTS
