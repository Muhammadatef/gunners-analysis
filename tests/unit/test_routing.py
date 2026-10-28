"""The deterministic router: what it decides without a model, and what it won't.

`fast_route` returning None is the safe outcome — the LM picks the question up.
A confident *wrong* route is a wrong answer, so the tests that matter most here
are the ones asserting it declines.
"""

import pytest

from agent import routing
from agent.tools import sql_registry


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------

def test_season_is_expanded_to_the_warehouse_form():
    assert routing.extract_season('how did we do in 2025/26') == '2025/2026'
    assert routing.extract_season('goals this season') == routing.DEFAULT_SEASON


def test_matchweek_from_several_spellings():
    assert routing.extract_matchweek('report for GW7') == 7
    assert routing.extract_matchweek('matchweek 22 please') == 22
    assert routing.extract_matchweek('no week here') is None


# ---------------------------------------------------------------------------
# fast routes
# ---------------------------------------------------------------------------

def test_refuses_questions_the_warehouse_cannot_answer():
    for question in ('is Saka injured?',
                     'what was the transfer fee for Gyokeres?',
                     'will Arsenal win the league next season?'):
        route = routing.fast_route(question)
        assert route is not None, question
        assert route.tool == 'none'


def test_top_scorers_needs_no_model():
    route = routing.fast_route('who are the top scorers this season?')
    assert route.tool == 'sql'
    assert route.query_id == 'top_scorers'
    assert route.parameters['season'] == routing.DEFAULT_SEASON


def test_leaderboard_families_the_cheap_tier_kept_missing():
    assert routing.fast_route(
        'most assists this season').query_id == 'top_by_metric'
    assert routing.fast_route(
        'who leads on progressive passes?').parameters['metric'] == \
        'prog_passes_per_90'
    assert routing.fast_route(
        'longest possession of the season').query_id == 'longest_sequences'


def test_season_scoped_questions():
    assert routing.fast_route('what was our PPDA this season?').query_id == \
        'season_pressing'
    assert routing.fast_route('home and away record').query_id == \
        'results_by_venue'
    assert routing.fast_route('how many points did Arsenal get in 2025/26?') \
        .query_id == 'season_summary'


def test_match_scoped_questions_carry_the_opponent():
    route = routing.fast_route('what was the xG against Man City?')
    assert route.query_id == 'match_xg'
    assert route.parameters['opponent'] == 'Man City'

    route = routing.fast_route('what was the score against Leeds?')
    assert route.query_id == 'match_result'
    assert route.parameters['opponent'] == 'Leeds'


def test_report_requests_route_to_the_report_skill():
    route = routing.fast_route('write a technical report for the Leeds match')
    assert route.tool == 'report'
    assert route.parameters['opponent'] == 'Leeds'


def test_report_without_a_resolvable_fixture_defers_to_the_model():
    assert routing.fast_route('write me a report') is None


def test_narrative_questions_go_to_retrieval():
    route = routing.fast_route('how did Arsenal set up in the second half?')
    assert route.tool == 'retrieval'


def test_unrecognised_questions_defer_to_the_model():
    assert routing.fast_route('') is None
    assert routing.fast_route('compare our first and second halves by tempo') \
        is None


def test_player_questions_use_warehouse_names(monkeypatch):
    monkeypatch.setattr(routing, 'known_players',
                        lambda schema=None: ['Bukayo Saka', 'Declan Rice'])
    route = routing.fast_route('how many goals did Saka score?')
    assert route.query_id == 'player_season_totals'
    assert route.parameters['player'] == 'Bukayo Saka'

    route = routing.fast_route('how many goals did Saka score against Leeds?')
    assert route.query_id == 'player_match_stats'
    assert route.parameters == {'player': 'Bukayo Saka', 'opponent': 'Leeds',
                                'season': routing.DEFAULT_SEASON}


def test_surnames_that_are_ordinary_words_are_not_matched_alone(monkeypatch):
    monkeypatch.setattr(routing, 'known_players',
                        lambda schema=None: ['Ben White'])
    route = routing.fast_route('how many goals were white shirts wearing')
    assert route is None or route.parameters.get('player') != 'Ben White'


# ---------------------------------------------------------------------------
# conversation slots
# ---------------------------------------------------------------------------

def test_follow_ups_inherit_the_previous_fixture():
    merged = routing.merge_slots(
        {}, {'opponent': 'Man City', 'season': '2025/2026'},
        'what about him in that match?')
    assert merged['opponent'] == 'Man City'


def test_a_fresh_question_does_not_inherit_an_identity():
    merged = routing.merge_slots(
        {}, {'opponent': 'Man City', 'player': 'Bukayo Saka'},
        'how many goals did Arsenal score against Leeds this season?')
    assert 'opponent' not in merged
    assert 'player' not in merged


def test_explicit_parameters_always_win():
    merged = routing.merge_slots(
        {'opponent': 'Leeds'}, {'opponent': 'Man City'}, 'what about there?')
    assert merged['opponent'] == 'Leeds'


def test_slots_from_a_stored_message_survive_json_text():
    slots = routing.slots_from_message(
        {'parameters': '{"opponent": "Leeds", "season": "2025/2026", "x": 1}'})
    assert slots == {'opponent': 'Leeds', 'season': '2025/2026'}


# ---------------------------------------------------------------------------
# catalogue shortlist
# ---------------------------------------------------------------------------

def test_shortlist_is_shorter_than_the_catalogue_and_keeps_pinned_ids():
    ids = routing.shortlist('how many goals did we score?', limit=4,
                            always=('season_summary',))
    assert len(ids) == 4
    assert ids[0] == 'season_summary'
    assert set(ids) <= set(sql_registry.REGISTRY)


def test_describe_renders_only_the_ids_it_was_given():
    text = sql_registry.describe(['match_xg', 'season_summary'])
    assert 'match_xg(' in text and 'season_summary(' in text
    assert 'top_carriers(' not in text
    assert len(text) < len(sql_registry.describe_all())


def test_describe_ignores_unknown_ids_rather_than_raising():
    assert sql_registry.describe(['not_a_query']) == sql_registry.describe_all()


@pytest.mark.parametrize('question', ['', 'xyzzy'])
def test_shortlist_never_returns_an_id_outside_the_allowlist(question):
    assert set(routing.shortlist(question)) <= set(sql_registry.REGISTRY)
