"""Data quality checks for a transformed match.

Importable so Airflow can gate on the result, and wrapped by quality/validate.py
for CLI use. Each check returns (passed, severity, detail); a run fails only if
an `error`-severity check fails, so cosmetic gaps (missing xG, thin qualifier
coverage) surface as warnings without blocking the dashboard.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

ERROR = 'error'
WARN = 'warning'

# A Premier League match is ~1200-2000 Opta events. Well outside that and the
# scrape almost certainly truncated.
MIN_EVENTS = 800
MAX_EVENTS = 3000
MIN_FULL_TIME_MINUTE = 90


@dataclass(frozen=True)
class Thresholds:
    """The competition-sensitive numbers, so they can be relaxed per run.

    Only event_count really varies: the defaults are calibrated to the Premier
    League, and a sparser feed (lower tiers, some non-European leagues) trips
    the minimum on a perfectly good match. The rest hold everywhere - the
    full-time check is already extra-time safe, and 3000 clears 120 minutes.
    """
    min_events: int = MIN_EVENTS
    max_events: int = MAX_EVENTS
    min_full_time_minute: int = MIN_FULL_TIME_MINUTE


def _check(name, passed, severity, detail):
    return {'name': name, 'passed': bool(passed), 'severity': severity, 'detail': detail}


def run_checks(events: pd.DataFrame, players: pd.DataFrame, raw: dict | None = None,
               thresholds: Thresholds | None = None) -> dict:
    t = thresholds or Thresholds()
    checks: list[dict[str, Any]] = []
    mcd = (raw or {}).get('matchCentreData', {})

    # --- completeness -------------------------------------------------------
    n = len(events)
    checks.append(_check('event_count_in_range', t.min_events <= n <= t.max_events, ERROR,
                         f'{n} events (expected {t.min_events}-{t.max_events})'))

    max_minute = int(events['minute'].max()) if n else 0
    checks.append(_check('covers_full_match', max_minute >= t.min_full_time_minute, ERROR,
                         f'last event at minute {max_minute}'))

    periods = set(events['period'].dropna().unique()) if n else set()
    checks.append(_check('both_halves_present',
                         {'FirstHalf', 'SecondHalf'}.issubset(periods), ERROR,
                         f'periods seen: {sorted(periods)}'))

    # --- referential integrity ---------------------------------------------
    teams = events['teamId'].dropna().unique()
    checks.append(_check('exactly_two_teams', len(teams) == 2, ERROR,
                         f'teamIds: {sorted(int(t) for t in teams)}'))

    if len(players):
        starters = players[players['isFirstEleven'] == True].groupby('teamId').size()  # noqa: E712
        checks.append(_check('eleven_starters_per_team',
                             len(starters) == 2 and (starters == 11).all(), ERROR,
                             f'starters per team: {starters.to_dict()}'))
        checks.append(_check('squad_size_sane', 20 <= len(players) <= 44, WARN,
                             f'{len(players)} players listed'))

    orphan = set(events['playerId'].dropna().astype(int)) - set(players['playerId'].astype(int))
    checks.append(_check('no_orphan_player_ids', not orphan, ERROR,
                         f'{len(orphan)} event player ids missing from the squad list'))

    # --- geometry (coordinates are already scaled to 105 x 68) --------------
    x_ok = events['x'].dropna().between(-0.5, 105.5).all()
    y_ok = events['y'].dropna().between(-0.5, 68.5).all()
    checks.append(_check('coordinates_within_pitch', x_ok and y_ok, ERROR,
                         f'x in range: {x_ok}, y in range: {y_ok}'))

    if 'endX' in events:
        end_ok = events['endX'].dropna().between(-10, 115).all()
        checks.append(_check('pass_end_coordinates_sane', end_ok, WARN,
                             'endX outside [-10, 115]' if not end_ok else 'ok'))

    # --- scoreline agreement ------------------------------------------------
    if mcd.get('ftScore'):
        try:
            fh, fa = (int(v.strip()) for v in mcd['ftScore'].split(':'))
            goals = int((events['type'] == 'Goal').sum())
            checks.append(_check('goal_events_match_scoreline', goals == fh + fa, ERROR,
                                 f'{goals} Goal events vs ftScore {fh}:{fa}'))
        except (ValueError, AttributeError):
            checks.append(_check('goal_events_match_scoreline', True, WARN,
                                 'ftScore unparseable, skipped'))

    # --- derived columns ----------------------------------------------------
    passes = events[events['type'] == 'Pass'] if n else events
    if len(passes):
        share = passes['xT'].notna().mean() if 'xT' in passes else 0.0
        checks.append(_check('xt_coverage', share >= 0.4, WARN,
                             f'xT present on {share:.0%} of passes'))
        checks.append(_check('pass_share_plausible', 0.4 <= len(passes) / n <= 0.85, WARN,
                             f'passes are {len(passes) / n:.0%} of events'))

    if 'pro' in events:
        prog = int((events['pro'] >= 9.144).sum())
        checks.append(_check('progressive_passes_plausible', 20 <= prog <= 250, WARN,
                             f'{prog} progressive passes across both teams'))

    failed_errors = [c for c in checks if not c['passed'] and c['severity'] == ERROR]
    failed_warns = [c for c in checks if not c['passed'] and c['severity'] == WARN]

    return {
        'status': 'PASS' if not failed_errors else 'FAIL',
        'thresholds': asdict(t),
        'passed': not failed_errors,
        'checks_total': len(checks),
        'checks_failed': len(failed_errors),
        'warnings': len(failed_warns),
        'checks': checks,
        'summary': (f'{len(checks) - len(failed_errors) - len(failed_warns)}/{len(checks)} passed, '
                    f'{len(failed_warns)} warning(s), {len(failed_errors)} error(s)'),
    }


def validate_job(job_id: str, root: str, thresholds: Thresholds | None = None) -> dict:
    """Run the checks for a processed job and write the report."""
    proc = os.path.join(root, 'data', 'processed', job_id)
    events = pd.read_csv(os.path.join(proc, 'EventData.csv'))
    players = pd.read_csv(os.path.join(proc, 'PlayerData.csv'))

    raw = None
    raw_path = os.path.join(root, 'data', 'raw', f'match_{job_id}.json')
    if os.path.exists(raw_path):
        with open(raw_path) as fh:
            raw = json.load(fh)

    report = run_checks(events, players, raw, thresholds)
    report['job_id'] = job_id

    out_dir = os.path.join(root, 'data', 'quality')
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f'{job_id}_report.json'), 'w') as fh:
        json.dump(report, fh, indent=2)
    return report
