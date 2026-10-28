"""Segmenting the event stream into possessions.

The rule that matters: a possession ends only when the opposition win
*controlled* possession. Their defensive contacts inside our attack — a block,
a challenge, a header won, a deflection — belong to our sequence, because that
is what they are: our attack, being resisted.

Getting this wrong is not subtle. The naive "consecutive events by one team"
rule collapsed a 14-event, 33-second Bournemouth goal to a single event,
because Saliba's deflected BallTouch sat between the cross and the finish.
"""

import pytest

from analytics.possession import (ends_possession, segment, summarise_team,
                                  zone_of)

ARSENAL, OPPONENT = 13, 19


def ev(index, team, kind, x=50.0, y=34.0, minute=0, second=0, *,
       outcome='Successful', period='FirstHalf', player=1, uid=None,
       is_goal=False, xt=0.0):
    return {
        'event_uid': uid if uid is not None else index + 1000,
        'event_index': index, 'team_id': team, 'type': kind,
        'outcome': outcome, 'x': x, 'y': y, 'minute': minute, 'second': second,
        'period': period, 'player_id': player, 'is_goal': is_goal,
        'is_shot': kind in ('Goal', 'SavedShot', 'MissedShots', 'ShotOnPost'),
        'xt_delta': xt, 'end_x': None, 'end_y': None,
    }


# --- what ends a possession -------------------------------------------------

def test_our_own_event_never_ends_our_possession():
    assert ends_possession(ev(0, ARSENAL, 'Pass'), ARSENAL) is False


def test_an_opposition_pass_ends_it():
    assert ends_possession(ev(0, OPPONENT, 'Pass'), ARSENAL) is True


@pytest.mark.parametrize('kind', ['BallTouch', 'Aerial', 'Challenge', 'Foul',
                                  'Save', 'Claim', 'Punch'])
def test_a_soft_contact_never_ends_it_even_when_successful(kind):
    """A won header or a save is the ball staying in open play."""
    assert ends_possession(ev(0, OPPONENT, kind, outcome='Successful'), ARSENAL) is False


@pytest.mark.parametrize('kind', ['Tackle', 'Interception', 'Clearance', 'BlockedPass'])
def test_a_failed_defensive_action_does_not_end_it(kind):
    assert ends_possession(ev(0, OPPONENT, kind, outcome='Unsuccessful'), ARSENAL) is False


@pytest.mark.parametrize('kind', ['Tackle', 'Interception'])
def test_a_successful_tackle_does_end_it(kind):
    assert ends_possession(ev(0, OPPONENT, kind, outcome='Successful'), ARSENAL) is True


def test_bookkeeping_events_end_nothing():
    for kind in ('SubstitutionOn', 'Card', 'FormationChange', 'End'):
        assert ends_possession(ev(0, OPPONENT, kind), ARSENAL) is False


# --- segmentation -----------------------------------------------------------

def test_a_simple_possession_is_one_sequence():
    events = [ev(i, ARSENAL, 'Pass', second=i) for i in range(5)]
    out = segment(events)
    assert len(out) == 1
    assert out[0]['events'] == 5
    assert out[0]['passes'] == 5
    assert out[0]['team_id'] == ARSENAL


def test_a_turnover_starts_a_new_sequence():
    events = ([ev(i, ARSENAL, 'Pass', second=i) for i in range(3)]
              + [ev(i, OPPONENT, 'Pass', second=i) for i in range(3, 6)])
    out = segment(events)
    assert [s['team_id'] for s in out] == [ARSENAL, OPPONENT]
    assert [s['events'] for s in out] == [3, 3]


def test_a_deflection_does_not_split_the_attack():
    """The Bournemouth case: a defender's touch between the cross and the goal."""
    events = [
        ev(0, ARSENAL, 'Pass', second=0),
        ev(1, ARSENAL, 'Pass', second=2),
        ev(2, OPPONENT, 'BallTouch', second=3),      # the deflection
        ev(3, ARSENAL, 'Goal', second=4, is_goal=True),
    ]
    out = segment(events)
    assert len(out) == 1
    assert out[0]['team_id'] == ARSENAL
    assert out[0]['events'] == 3          # the opposition touch is not ours
    assert out[0]['ended_in_goal'] is True


def test_half_time_is_a_hard_boundary():
    events = [ev(0, ARSENAL, 'Pass', period='FirstHalf'),
              ev(1, ARSENAL, 'Pass', period='SecondHalf')]
    out = segment(events)
    assert len(out) == 2


def test_bookkeeping_events_are_not_counted_in_a_sequence():
    events = [ev(0, ARSENAL, 'Pass'), ev(1, ARSENAL, 'SubstitutionOn'),
              ev(2, ARSENAL, 'Pass')]
    out = segment(events)
    assert len(out) == 1 and out[0]['events'] == 2


def test_an_empty_stream_yields_nothing():
    assert segment([]) == []


# --- what a sequence reports ------------------------------------------------

def test_progression_is_measured_to_the_furthest_point_reached():
    """Not to where it ended — a move that reaches the box and is cleared
    still progressed to the box."""
    events = [ev(0, ARSENAL, 'Pass', x=20.0, second=0),
              ev(1, ARSENAL, 'Pass', x=90.0, second=3),
              ev(2, ARSENAL, 'Pass', x=60.0, second=5)]
    out = segment(events)[0]
    assert out['max_x'] == 90.0
    assert out['progression_m'] == 70.0


def test_a_sequence_records_its_shot_and_duration():
    events = [ev(0, ARSENAL, 'Pass', second=10),
              ev(1, ARSENAL, 'SavedShot', second=18)]
    out = segment(events)[0]
    assert out['ended_in_shot'] is True
    assert out['ended_in_goal'] is False
    assert out['duration_s'] == 8.0


def test_distinct_players_are_counted():
    events = [ev(0, ARSENAL, 'Pass', player=1), ev(1, ARSENAL, 'Pass', player=2),
              ev(2, ARSENAL, 'Pass', player=1)]
    assert segment(events)[0]['players'] == 2


def test_a_high_turnover_is_flagged_by_where_it_was_won():
    won_high = segment([ev(0, ARSENAL, 'BallRecovery', x=80.0),
                        ev(1, ARSENAL, 'Pass', x=85.0)])[0]
    assert won_high['is_high_turnover'] is True
    assert won_high['build_zone'] == 'attacking'

    won_deep = segment([ev(0, ARSENAL, 'Pass', x=20.0)])[0]
    assert won_deep['is_high_turnover'] is False
    assert won_deep['build_zone'] == 'defensive'


@pytest.mark.parametrize('x,expected', [
    (0.0, 'defensive'), (34.9, 'defensive'), (35.1, 'middle'),
    (69.9, 'middle'), (70.1, 'attacking'), (105.0, 'attacking'),
    (None, 'unknown'),
])
def test_zone_boundaries(x, expected):
    assert zone_of(x) == expected


# --- team roll-up -----------------------------------------------------------

def test_team_summary_counts_only_that_team():
    sequences = segment(
        [ev(i, ARSENAL, 'Pass', second=i) for i in range(12)]
        + [ev(12, OPPONENT, 'Pass', second=12)])
    out = summarise_team(sequences, ARSENAL)
    assert out['sequences'] == 1
    assert out['long_sequences'] == 1          # 12 events >= 10
    assert out['avg_sequence_events'] == 12.0


def test_team_summary_on_no_sequences_is_empty_not_an_error():
    out = summarise_team([], ARSENAL)
    assert out['sequences'] == 0
    assert out['avg_sequence_events'] is None
