"""The calculator as a post-step: computed from evidence, or not at all."""

from agent.tools import python_tool


SEASON_ROW = {'played': 38, 'won': 26, 'goals_for': 76, 'goals_against': 29,
              'points': 84, 'total_xg': 71.4}
PLAYER_ROW = {'player_name': 'Bukayo Saka', 'appearances': 34, 'minutes': 2700,
              'goals': 12, 'assists': 9, 'passes': 1200,
              'passes_accurate': 1020}


def test_goals_per_game_is_computed_not_guessed():
    calc = python_tool.derive('how many goals per game did Arsenal score?',
                              [SEASON_ROW])
    assert calc is not None
    assert calc.result == round(76 / 38, 2)
    assert 'per match' in calc.explanation


def test_per_90_uses_minutes_rather_than_appearances():
    calc = python_tool.derive("what are Saka's goals per 90?", [PLAYER_ROW])
    assert calc.result == round(12 / (2700 / 90), 2)


def test_pass_accuracy_is_a_percentage():
    calc = python_tool.derive("what was Saka's passing accuracy?", [PLAYER_ROW])
    assert calc.result == round(100 * 1020 / 1200, 1)


def test_goals_conceded_is_not_read_as_goals_scored():
    calc = python_tool.derive('goals conceded per game', [SEASON_ROW])
    assert calc.result == round(29 / 38, 2)


def test_no_ratio_asked_for_means_no_calculation():
    assert python_tool.derive('how many goals did Arsenal score?',
                              [SEASON_ROW]) is None


def test_a_missing_denominator_declines_rather_than_improvises():
    assert python_tool.derive('goals per game', [{'goals_for': 76}]) is None


def test_no_evidence_means_no_calculation():
    assert python_tool.derive('goals per game', []) is None
    assert python_tool.derive('goals per game', None) is None


def test_the_expression_carries_its_own_provenance():
    calc = python_tool.derive('points per game', [SEASON_ROW])
    assert '84' in calc.as_prompt() and '38' in calc.as_prompt()
    assert calc.as_prompt().startswith('[python]')
