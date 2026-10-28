"""Reconstruct carries — the ball moving at a player's feet.

Opta records where an event starts and, for a pass, where it ends. It does not
record the run in between: if a defender receives a pass on halfway and drives
twenty metres before playing it, the feed shows a pass arriving at halfway and
another leaving from the edge of the final third, with the twenty metres
invisible. `db/readiness.py` calls this the missing `carries` dimension and
notes it "must be derived from consecutive touches".

The derivation is the gap between where the ball last was and where the same
player next used it:

  * **after a pass** — the receiver's first action starts somewhere other than
    where the pass landed; the difference is their carry.
  * **between two of a player's own actions** — a touch followed by a shot from
    a different spot is a carry into the shooting position.

Filters exist to stop noise being dressed up as ball progression:

  * shorter than `MIN_CARRY_M` is measurement jitter, not a carry;
  * longer than `MAX_CARRY_M` in the time available is a tracking gap or a
    mislabelled event, not Messi;
  * a possession change between the two events means the ball was not carried,
    it was lost.

A carry is an inference, not an observation, and it is stored in its own table
rather than mixed into `events` so nobody mistakes one for the other.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from analytics.possession import NEUTRAL, _get, _num, zone_of

MIN_CARRY_M = 3.0        # below this it is standing still
MAX_CARRY_M = 60.0       # above this the feed has a gap
MAX_CARRY_S = 15.0       # nobody dribbles unopposed for longer
MAX_SPEED_MS = 12.0      # faster than a sprint means bad data

# The ball has to be at the player's feet for a carry to follow.
RECEIVABLE = frozenset({'Pass', 'BallRecovery', 'Interception', 'TakeOn',
                        'BallTouch', 'Tackle', 'Clearance', 'Aerial',
                        'CornerAwarded', 'Dispossessed', 'Challenge'})

# A carry ends when the player does something with the ball.
RELEASE = frozenset({'Pass', 'Goal', 'SavedShot', 'MissedShots', 'ShotOnPost',
                     'TakeOn', 'Dispossessed', 'Foul', 'BallTouch',
                     'Clearance', 'CornerAwarded'})


def _clock(event) -> float:
    return _num(_get(event, 'minute')) * 60 + _num(_get(event, 'second'))


def _origin(event) -> tuple[float | None, float | None]:
    """Where the ball ended up after this event."""
    ex, ey = _get(event, 'end_x'), _get(event, 'end_y')
    if ex is not None and ey is not None:
        return _num(ex, None), _num(ey, None)
    return _num(_get(event, 'x'), None), _num(_get(event, 'y'), None)


def carries(events: Sequence[Any]) -> list[dict]:
    """One row per inferred carry, in match order."""
    out: list[dict] = []
    playable = [e for e in events if str(_get(e, 'type') or '') not in NEUTRAL]

    for previous, current in zip(playable, playable[1:]):
        kind_prev = str(_get(previous, 'type') or '')
        kind_cur = str(_get(current, 'type') or '')

        if kind_prev not in RECEIVABLE or kind_cur not in RELEASE:
            continue
        # Same team, or the ball changed hands and nothing was carried.
        if _get(previous, 'team_id') != _get(current, 'team_id'):
            continue
        if _get(previous, 'period') != _get(current, 'period'):
            continue

        carrier = _get(current, 'player_id')
        if carrier is None:
            continue
        # Either the same player kept the ball, or they received the pass.
        same_player = _get(previous, 'player_id') == carrier
        received = kind_prev == 'Pass' and str(_get(previous, 'outcome')) == 'Successful'
        if not (same_player or received):
            continue

        sx, sy = _origin(previous)
        ex, ey = _num(_get(current, 'x'), None), _num(_get(current, 'y'), None)
        if None in (sx, sy, ex, ey):
            continue

        distance = math.hypot(ex - sx, ey - sy)
        if distance < MIN_CARRY_M or distance > MAX_CARRY_M:
            continue

        duration = _clock(current) - _clock(previous)
        if duration < 0 or duration > MAX_CARRY_S:
            continue
        # A whole-second clock makes 0s carries common and their speed
        # meaningless, so only police speed when there is time to divide by.
        if duration > 0 and distance / duration > MAX_SPEED_MS:
            continue

        out.append({
            'event_uid': _get(current, 'event_uid'),
            'team_id': _get(current, 'team_id'),
            'player_id': carrier,
            'period': _get(current, 'period'),
            'minute': int(_num(_get(current, 'minute'))),
            'second': int(_num(_get(current, 'second'))),
            'start_x': round(sx, 2), 'start_y': round(sy, 2),
            'end_x': round(ex, 2), 'end_y': round(ey, 2),
            'distance_m': round(distance, 2),
            # Only forward movement counts as progression.
            'progression_m': round(max(ex - sx, 0.0), 2),
            'duration_s': round(duration, 1),
            'start_zone': zone_of(sx),
            'end_zone': zone_of(ex),
            'is_progressive': (ex - sx) >= 9.144,
            'ends_in_shot': kind_cur in {'Goal', 'SavedShot', 'MissedShots',
                                         'ShotOnPost'},
        })
    return out
