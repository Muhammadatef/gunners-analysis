"""PPDA, counterpresses and inferred carries.

Two things here are easy to get silently wrong and expensive to notice later:

- **PPDA's coordinate frames.** Opta records every event in the *acting* team's
  attacking direction, so the same strip of grass has different x for the two
  sides. Mirror it the wrong way and the metric inverts — a pressing side reads
  as passive and nothing about the number looks obviously wrong.
- **Carries are inferred, not observed.** The filters are what stop noise being
  presented as ball progression.
"""

import pytest

from analytics.carries import MAX_CARRY_M, MIN_CARRY_M, carries
from analytics.pressing import COUNTERPRESS_WINDOW_S, ppda, pressure_events

ARSENAL, OPPONENT = 13, 19


def ev(index, team, kind, x=50.0, y=34.0, minute=0, second=0, *,
       outcome='Successful', period='FirstHalf', player=1, end_x=None, end_y=None):
    return {
        'event_uid': index + 1000, 'event_index': index, 'team_id': team,
        'type': kind, 'outcome': outcome, 'x': x, 'y': y, 'minute': minute,
        'second': second, 'period': period, 'player_id': player,
        'end_x': end_x, 'end_y': end_y, 'is_goal': False, 'is_shot': False,
        'xt_delta': 0.0,
    }


# --- PPDA -------------------------------------------------------------------

def test_ppda_divides_opponent_passes_by_our_defensive_actions():
    # 10 opponent passes in their own 60% (x < 63 in their frame),
    # 2 Arsenal defensive actions in the same strip (x > 42 in ours).
    events = ([ev(i, OPPONENT, 'Pass', x=30.0) for i in range(10)]
              + [ev(10, ARSENAL, 'Tackle', x=60.0),
                 ev(11, ARSENAL, 'Interception', x=70.0)])
    value, actions = ppda(events, ARSENAL, OPPONENT)
    assert actions == 2
    assert value == 5.0


def test_opponent_passes_in_their_final_40_percent_are_excluded():
    """A pass deep in their attacking third is not something we were pressing."""
    events = ([ev(i, OPPONENT, 'Pass', x=90.0) for i in range(10)]
              + [ev(10, ARSENAL, 'Tackle', x=60.0)])
    value, _ = ppda(events, ARSENAL, OPPONENT)
    assert value is None          # no opponent passes counted in the zone


def test_our_defensive_actions_in_our_own_third_are_excluded():
    """A tackle on our own goal line is defending, not pressing."""
    events = ([ev(i, OPPONENT, 'Pass', x=30.0) for i in range(10)]
              + [ev(10, ARSENAL, 'Tackle', x=10.0)])   # deep in our half
    value, actions = ppda(events, ARSENAL, OPPONENT)
    assert actions == 0 and value is None


def test_ppda_is_none_rather_than_a_division_by_zero():
    assert ppda([], ARSENAL, OPPONENT) == (None, 0)


def test_lower_ppda_means_more_pressing():
    """Sanity on direction: more defensive actions for the same passes is lower."""
    passes = [ev(i, OPPONENT, 'Pass', x=30.0) for i in range(20)]
    passive, _ = ppda(passes + [ev(99, ARSENAL, 'Tackle', x=60.0)], ARSENAL, OPPONENT)
    intense, _ = ppda(passes + [ev(90 + i, ARSENAL, 'Tackle', x=60.0)
                                for i in range(5)], ARSENAL, OPPONENT)
    assert intense < passive


@pytest.mark.parametrize('kind', ['Tackle', 'Interception', 'Challenge', 'Foul'])
def test_the_four_ppda_actions_all_count(kind):
    events = [ev(0, OPPONENT, 'Pass', x=30.0), ev(1, ARSENAL, kind, x=60.0)]
    _, actions = ppda(events, ARSENAL, OPPONENT)
    assert actions == 1


def test_a_clearance_is_not_a_ppda_action():
    """Clearing your lines is not pressing, however defensive it looks."""
    events = [ev(0, OPPONENT, 'Pass', x=30.0), ev(1, ARSENAL, 'Clearance', x=60.0)]
    _, actions = ppda(events, ARSENAL, OPPONENT)
    assert actions == 0


# --- counterpressing --------------------------------------------------------

def test_a_defensive_action_soon_after_losing_the_ball_is_a_counterpress():
    sequences = [{'team_id': ARSENAL, 'end_minute': 10, 'end_second': 0}]
    events = [ev(0, ARSENAL, 'Tackle', minute=10, second=3)]
    out = pressure_events(events, sequences)
    assert out[0]['is_counterpress'] is True
    assert out[0]['seconds_since_loss'] == 3.0


def test_a_defensive_action_long_after_losing_it_is_not():
    sequences = [{'team_id': ARSENAL, 'end_minute': 10, 'end_second': 0}]
    events = [ev(0, ARSENAL, 'Tackle', minute=10,
                 second=int(COUNTERPRESS_WINDOW_S) + 10)]
    assert pressure_events(events, sequences)[0]['is_counterpress'] is False


def test_only_defensive_actions_become_pressure_events():
    events = [ev(0, ARSENAL, 'Pass'), ev(1, ARSENAL, 'Tackle')]
    out = pressure_events(events, [])
    assert len(out) == 1 and out[0]['type'] == 'Tackle'


# --- carries ----------------------------------------------------------------

def test_a_player_moving_with_the_ball_is_a_carry():
    events = [ev(0, ARSENAL, 'BallRecovery', x=30.0, y=34.0, player=7, second=0),
              ev(1, ARSENAL, 'Pass', x=50.0, y=34.0, player=7, second=3)]
    out = carries(events)
    assert len(out) == 1
    assert out[0]['distance_m'] == 20.0
    assert out[0]['progression_m'] == 20.0
    assert out[0]['is_progressive'] is True
    assert out[0]['player_id'] == 7


def test_the_receiver_of_a_pass_can_carry_it():
    events = [ev(0, ARSENAL, 'Pass', x=20.0, y=34.0, player=5, second=0,
                 end_x=40.0, end_y=34.0),
              ev(1, ARSENAL, 'Pass', x=55.0, y=34.0, player=8, second=3)]
    out = carries(events)
    assert len(out) == 1
    assert out[0]['player_id'] == 8
    assert out[0]['start_x'] == 40.0      # from where the pass landed
    assert out[0]['distance_m'] == 15.0


def test_a_tiny_movement_is_not_a_carry():
    events = [ev(0, ARSENAL, 'BallRecovery', x=30.0, player=7, second=0),
              ev(1, ARSENAL, 'Pass', x=30.0 + MIN_CARRY_M - 0.5, player=7, second=1)]
    assert carries(events) == []


def test_an_impossibly_long_carry_is_rejected():
    events = [ev(0, ARSENAL, 'BallRecovery', x=5.0, player=7, second=0),
              ev(1, ARSENAL, 'Pass', x=5.0 + MAX_CARRY_M + 10, player=7, second=5)]
    assert carries(events) == []


def test_an_impossibly_fast_carry_is_rejected():
    """40 m in one second is a feed gap, not a sprint."""
    events = [ev(0, ARSENAL, 'BallRecovery', x=20.0, player=7, second=0),
              ev(1, ARSENAL, 'Pass', x=60.0, player=7, second=1)]
    assert carries(events) == []


def test_the_ball_changing_hands_is_not_a_carry():
    events = [ev(0, ARSENAL, 'BallRecovery', x=30.0, player=7, second=0),
              ev(1, OPPONENT, 'Pass', x=50.0, player=9, second=3)]
    assert carries(events) == []


def test_a_carry_across_half_time_is_not_a_carry():
    events = [ev(0, ARSENAL, 'BallRecovery', x=30.0, player=7, second=0,
                 period='FirstHalf'),
              ev(1, ARSENAL, 'Pass', x=50.0, player=7, second=3,
                 period='SecondHalf')]
    assert carries(events) == []


def test_backwards_movement_progresses_nothing():
    events = [ev(0, ARSENAL, 'BallRecovery', x=60.0, player=7, second=0),
              ev(1, ARSENAL, 'Pass', x=40.0, player=7, second=3)]
    out = carries(events)
    assert out[0]['distance_m'] == 20.0
    assert out[0]['progression_m'] == 0.0
    assert out[0]['is_progressive'] is False
