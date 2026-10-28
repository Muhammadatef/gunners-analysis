"""Segment a match's event stream into possession sequences.

Opta emits no possession id, so sequences have to be derived. The naive rule -
"a sequence is a run of consecutive events by one team" - collapses almost
every attack to a single event, because the opposition's defensive contacts are
interleaved all the way through: a block, a challenge, a header won, a
deflection off a defender. In the Bournemouth goal that first exposed this,
Saliba's deflected `BallTouch` sits between the cross and the finish.

So a sequence ends only when the opposition win *controlled* possession. Their
contacts inside our attack belong to our sequence, because that is what they
are: our attack, being resisted.

This is the segmentation `db/readiness.py` calls the missing `possession_chains`
dimension, and it is also what `visualizer/goal_animation.py` uses to animate a
goal - that module imports from here rather than keeping a second copy.

    from analytics.possession import segment
    sequences = segment(events)          # events ordered by event_index
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Opposition events that are a contact inside our possession, not a turnover.
DEFENSIVE_CONTACT = frozenset({
    'BallTouch', 'Tackle', 'Challenge', 'Aerial', 'Foul', 'Save', 'BlockedPass',
    'Interception', 'Clearance', 'CornerAwarded', 'Claim', 'Punch',
    'KeeperPickup', 'Smother', 'CrossNotClaimed',
})

# These never end a possession even when the feed marks them successful: a won
# header or a successful block is still the ball staying in open play.
ALWAYS_SOFT = frozenset({
    'BallTouch', 'Aerial', 'Challenge', 'Foul', 'Save', 'Claim', 'Punch',
})

# Bookkeeping rather than football. They own nothing and end nothing.
NEUTRAL = frozenset({
    'SubstitutionOn', 'SubstitutionOff', 'Card', 'FormationSet',
    'FormationChange', 'Start', 'End', 'TeamSetUp', 'CollectionEnd',
    'DelayedStart', 'DelayEnd',
})

# What a sequence can be said to have produced.
SHOT_TYPES = frozenset({'Goal', 'SavedShot', 'MissedShots', 'ShotOnPost'})

# On a 105 m pitch.
PITCH_LENGTH = 105.0
DEFENSIVE_THIRD = PITCH_LENGTH / 3
ATTACKING_THIRD = PITCH_LENGTH * 2 / 3


def _get(event: Any, key: str, default=None):
    """Events arrive as dicts from psycopg2 or as pandas rows. Accept both."""
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock(event) -> float:
    return _num(_get(event, 'minute')) * 60 + _num(_get(event, 'second'))


def ends_possession(event, holder_team) -> bool:
    """Has the opposition actually taken the ball off us with this event?"""
    team = _get(event, 'team_id')
    if team == holder_team:
        return False
    kind = str(_get(event, 'type') or '')
    if kind in NEUTRAL:
        return False
    if kind in DEFENSIVE_CONTACT:
        if kind in ALWAYS_SOFT:
            return False
        return str(_get(event, 'outcome') or '') == 'Successful'
    return True


def zone_of(x: float | None) -> str:
    if x is None:
        return 'unknown'
    if x < DEFENSIVE_THIRD:
        return 'defensive'
    if x < ATTACKING_THIRD:
        return 'middle'
    return 'attacking'


def segment(events: Sequence[Any]) -> list[dict]:
    """Split an ordered event stream into possession sequences.

    Returns one dict per sequence, in match order, each carrying the summary
    fields `possession_sequences` stores plus `event_uids` for the caller.
    """
    sequences: list[dict] = []
    current: dict | None = None

    def close():
        nonlocal current
        if current and current['own_events']:
            sequences.append(_summarise(current, len(sequences)))
        current = None

    for event in events:
        kind = str(_get(event, 'type') or '')
        team = _get(event, 'team_id')

        if kind in NEUTRAL or team is None:
            continue

        if current is None:
            current = _open(event, team)
        elif _get(event, 'period') != current['period']:
            # Half time is a hard boundary; nothing carries across it.
            close()
            current = _open(event, team)
        elif ends_possession(event, current['team_id']):
            close()
            current = _open(event, team)

        current['all_events'].append(event)
        if team == current['team_id']:
            current['own_events'].append(event)

    close()
    return sequences


def _open(event, team_id) -> dict:
    return {
        'team_id': team_id,
        'period': _get(event, 'period'),
        'all_events': [],
        'own_events': [],
    }


def _summarise(seq: dict, index: int) -> dict:
    own = seq['own_events']
    everything = seq['all_events']
    first, last = own[0], everything[-1]

    xs = [_num(_get(e, 'x'), None) for e in own if _get(e, 'x') is not None]
    start_x = _num(_get(first, 'x'), 0.0)
    max_x = max(xs) if xs else start_x

    duration = max(_clock(last) - _clock(first), 0.0)
    players = {_get(e, 'player_id') for e in own if _get(e, 'player_id') is not None}

    shot = next((e for e in own if str(_get(e, 'type')) in SHOT_TYPES), None)
    goal = next((e for e in own if _get(e, 'is_goal')), None)

    return {
        'sequence_index': index,
        'team_id': seq['team_id'],
        'period': seq['period'],
        'start_event_uid': _get(first, 'event_uid'),
        'end_event_uid': _get(last, 'event_uid'),
        'start_minute': int(_num(_get(first, 'minute'))),
        'start_second': int(_num(_get(first, 'second'))),
        'end_minute': int(_num(_get(last, 'minute'))),
        'end_second': int(_num(_get(last, 'second'))),
        'duration_s': round(duration, 1),
        'events': len(own),
        'passes': sum(1 for e in own if str(_get(e, 'type')) == 'Pass'),
        'players': len(players),
        'start_x': round(start_x, 2),
        'start_y': round(_num(_get(first, 'y')), 2),
        'end_x': round(_num(_get(last, 'x')), 2),
        'end_y': round(_num(_get(last, 'y')), 2),
        'max_x': round(max_x, 2),
        'progression_m': round(max_x - start_x, 2),
        'xt_sum': round(sum(_num(_get(e, 'xt_delta')) for e in own), 5),
        'start_type': str(_get(first, 'type') or ''),
        'build_zone': zone_of(start_x),
        'is_high_turnover': start_x >= ATTACKING_THIRD,
        'end_type': str(_get(last, 'type') or ''),
        'ended_in_shot': shot is not None,
        'ended_in_goal': goal is not None,
        'event_uids': [_get(e, 'event_uid') for e in own],
    }


def chain_ending_at(events: Sequence[Any], position: int) -> list[Any]:
    """The single possession that ends at `position` - used to animate a goal.

    Same rule as `segment`, walked backwards from one event rather than forwards
    over the whole match.
    """
    team = _get(events[position], 'team_id')
    i = position
    while i > 0:
        previous = events[i - 1]
        if _get(previous, 'team_id') == team:
            i -= 1
            continue
        if not ends_possession(previous, team):
            i -= 1
            continue
        break
    return list(events[i:position + 1])


def summarise_team(sequences: Iterable[dict], team_id) -> dict:
    """Sequence-shape features for one team, for match_tactical_features."""
    own = [s for s in sequences if s['team_id'] == team_id]
    if not own:
        return {'sequences': 0, 'avg_sequence_events': None,
                'avg_sequence_duration_s': None, 'direct_speed_ms': None,
                'long_sequences': 0, 'sequences_ending_in_shot': 0,
                'avg_start_x': None, 'build_from_defensive_third': 0,
                'build_from_high_turnover': 0}

    total_time = sum(s['duration_s'] for s in own)
    total_progress = sum(s['progression_m'] for s in own)
    return {
        'sequences': len(own),
        'avg_sequence_events': round(sum(s['events'] for s in own) / len(own), 2),
        'avg_sequence_duration_s': round(total_time / len(own), 2),
        # Metres gained upfield per second of possession: the usual proxy for
        # whether a side plays direct or patiently.
        'direct_speed_ms': round(total_progress / total_time, 3) if total_time else None,
        'long_sequences': sum(1 for s in own if s['events'] >= 10),
        'sequences_ending_in_shot': sum(1 for s in own if s['ended_in_shot']),
        'avg_start_x': round(sum(s['start_x'] for s in own) / len(own), 2),
        'build_from_defensive_third': sum(1 for s in own if s['build_zone'] == 'defensive'),
        'build_from_high_turnover': sum(1 for s in own if s['is_high_turnover']),
    }
