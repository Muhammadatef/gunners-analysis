"""Pressing: defensive engagements, counterpresses, and PPDA.

Opta emits no pressure event — a fact `db/readiness.py` records as the reason
its `pressing_events` dimension is missing. What Opta does emit is defensive
actions, and PPDA is defined over exactly those, so pressing is reconstructed
from them rather than invented.

PPDA follows the definition in `docs/FOOTBALL_DATA_COOKBOOK.md`:

    PPDA = opponent passes in their own 60% of the pitch
           ÷ (our tackles + interceptions + challenges + fouls there)

Lower means more intense pressing; elite pressing sides run 7–9, passive sides
15 and up. "Their own 60%" is the zone a pressing side actually contests — the
final 40% is excluded because a tackle on your own goal line is defending, not
pressing, and counting it would flatter a team that sits deep.

Note the two coordinate frames. Opta records every event in the *acting* team's
attacking direction, so a defensive action at x = 30 by the pressing team
happens 30 m into their own half — which is 75 m from the opponent's goal, and
therefore in the opponent's build-up zone. The mirror is applied explicitly
below rather than left implicit, because getting it backwards silently inverts
the metric.
"""

from __future__ import annotations

from typing import Any, Sequence

from analytics.possession import (NEUTRAL, PITCH_LENGTH, _get, _num, zone_of)

# The four actions PPDA counts.
PPDA_ACTIONS = frozenset({'Tackle', 'Interception', 'Challenge', 'Foul'})

# Everything that counts as a defensive engagement for the wider table.
DEFENSIVE_ACTIONS = frozenset({
    'Tackle', 'Interception', 'Challenge', 'Foul', 'BlockedPass', 'Clearance',
    'BallRecovery', 'Aerial',
})

# PPDA's pitch restriction: the opponent's own 60%, i.e. everything except the
# final 40% nearest the goal they are attacking.
PPDA_ZONE_LIMIT = PITCH_LENGTH * 0.6

# A defensive action within this many seconds of losing the ball is a
# counterpress — winning it back immediately rather than retreating.
COUNTERPRESS_WINDOW_S = 5.0


def _clock(event) -> float:
    return _num(_get(event, 'minute')) * 60 + _num(_get(event, 'second'))


def pressure_events(events: Sequence[Any], sequences: Sequence[dict]) -> list[dict]:
    """One row per defensive engagement, with the context to aggregate it.

    `sequences` supplies when each team lost the ball, which is what makes a
    counterpress identifiable.
    """
    # When did each team's possession end? Used for the counterpress window.
    lost_at: dict[Any, list[float]] = {}
    for s in sequences:
        lost_at.setdefault(s['team_id'], []).append(
            s['end_minute'] * 60 + s['end_second'])
    for times in lost_at.values():
        times.sort()

    teams = {t for t in (_get(e, 'team_id') for e in events) if t is not None}

    out: list[dict] = []
    for event in events:
        kind = str(_get(event, 'type') or '')
        if kind in NEUTRAL or kind not in DEFENSIVE_ACTIONS:
            continue
        team = _get(event, 'team_id')
        if team is None:
            continue

        x = _num(_get(event, 'x'), None)
        when = _clock(event)

        # How long since this team last lost the ball?
        since = None
        for t in lost_at.get(team, []):
            if t <= when:
                since = when - t
            else:
                break

        opponent = next((t for t in teams if t != team), None)
        out.append({
            'event_uid': _get(event, 'event_uid'),
            'team_id': team,
            'player_id': _get(event, 'player_id'),
            'opponent_team_id': opponent,
            'minute': int(_num(_get(event, 'minute'))),
            'second': int(_num(_get(event, 'second'))),
            'period': _get(event, 'period'),
            'type': kind,
            'outcome': _get(event, 'outcome'),
            'x': None if x is None else round(x, 2),
            'y': round(_num(_get(event, 'y')), 2),
            'third': zone_of(x),
            'is_counterpress': since is not None and since <= COUNTERPRESS_WINDOW_S,
            'seconds_since_loss': None if since is None else round(since, 1),
        })
    return out


def ppda(events: Sequence[Any], pressing_team, opponent_team) -> tuple[float | None, int]:
    """(PPDA for `pressing_team`, defensive actions counted).

    Returns None when the opponent attempted no passes in the zone, which
    happens in a fragment of a match rather than a full one — a division by
    zero dressed up as a number would be worse than an honest gap.
    """
    opponent_passes = 0
    defensive_actions = 0

    for event in events:
        kind = str(_get(event, 'type') or '')
        team = _get(event, 'team_id')
        x = _num(_get(event, 'x'), None)
        if x is None:
            continue

        if team == opponent_team and kind == 'Pass':
            # The opponent's own coordinates: their own 60% is x < 63.
            if x < PPDA_ZONE_LIMIT:
                opponent_passes += 1

        elif team == pressing_team and kind in PPDA_ACTIONS:
            # Our coordinates are mirrored relative to theirs, so the same
            # strip of grass is x > 42 for us.
            if x > PITCH_LENGTH - PPDA_ZONE_LIMIT:
                defensive_actions += 1

    if defensive_actions == 0:
        return None, 0
    if opponent_passes == 0:
        return None, defensive_actions
    return round(opponent_passes / defensive_actions, 2), defensive_actions


def team_pressing_features(events: Sequence[Any], pressures: Sequence[dict],
                           team_id, opponent_id) -> dict:
    value, actions = ppda(events, team_id, opponent_id)
    against, _ = ppda(events, opponent_id, team_id)
    own = [p for p in pressures if p['team_id'] == team_id]
    return {
        'ppda': value,
        'ppda_allowed': against,
        'def_actions_opp_half': actions,
        'counterpresses': sum(1 for p in own if p['is_counterpress']),
        'high_turnovers': sum(
            1 for p in own
            if p['third'] == 'attacking'
            and p['type'] in ('BallRecovery', 'Interception')),
    }
