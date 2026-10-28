"""Classifier output sanity — catch DSPy adapter junk from weak models."""

from types import SimpleNamespace

from agent.modules import route_looks_broken


def test_valid_sql_route_ok():
    assert not route_looks_broken(SimpleNamespace(
        tool='sql', query_id='season_summary', parameters='{}', reasoning='ok'))


def test_valid_none_route_ok():
    assert not route_looks_broken(SimpleNamespace(
        tool='none', query_id='', parameters='{}', reasoning='future season'))


def test_adapter_junk_query_id():
    assert route_looks_broken(SimpleNamespace(
        tool='none', query_id='SINGLE-RESULT-END', parameters='{}', reasoning='\n'))


def test_sql_without_query_id():
    assert route_looks_broken(SimpleNamespace(
        tool='sql', query_id='', parameters='{}', reasoning='points'))


def test_unknown_tool():
    assert route_looks_broken(SimpleNamespace(
        tool='calculator', query_id='', parameters='{}', reasoning='x'))


# ---------------------------------------------------------------------------
# one classify per request
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from agent import routing  # noqa: E402
from agent.modules import FootballAnalyst, IntentClassifier, as_route  # noqa: E402


class _Exploding:
    """Anything the request should not have paid for."""

    def __init__(self, what='the LM'):
        self.what = what

    def __call__(self, *_, **__):
        raise AssertionError(f'{self.what} was called and should not have been')


class _StubRetriever:
    def search(self, *_, **__):
        return []


def _analyst(**kwargs):
    agent = FootballAnalyst(retriever=_StubRetriever(), **kwargs)
    agent.analyst = lambda question, evidence: SimpleNamespace(
        answer='Arsenal scored 76.')
    return agent


def test_a_supplied_route_is_not_classified_again():
    """`/chat` routes before it knows which branch to take; the agent it hands
    the route to must not pay for that decision a second time."""
    agent = _analyst()
    agent.classifier = _Exploding('the classifier')
    agent._run_sql = lambda query_id, params: (None, 'no database here')

    pred = agent(question='how many goals did Arsenal score?',
                 route=routing.Route('sql', 'season_summary',
                                     {'team': 'Arsenal', 'season': '2025/2026'},
                                     'season record', source='caller'))
    assert pred.query_id == 'season_summary'
    assert pred.route_source == 'caller'


def test_the_fast_path_answers_without_an_lm():
    classifier = IntentClassifier()
    classifier.classify = _Exploding()
    route = classifier(question='who are the top scorers this season?')
    assert route.query_id == 'top_scorers'
    assert route.source == 'fast'


def test_disabling_the_fast_path_falls_back_to_the_model():
    classifier = IntentClassifier(fast_path=False)
    classifier.classify = lambda **_: SimpleNamespace(
        tool='sql', query_id='top_scorers', parameters='{"team": "Arsenal"}',
        reasoning='leaders')
    route = classifier(question='who are the top scorers this season?')
    assert route.source == 'lm'
    assert route.parameters == {'team': 'Arsenal'}


def test_the_classifier_is_shown_a_shortlist_not_the_whole_catalogue():
    seen = {}

    classifier = IntentClassifier(fast_path=False)

    def _capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(tool='sql', query_id='match_xg',
                               parameters='{}', reasoning='xg')

    classifier.classify = _capture
    classifier(question='what was the xG against Man City?')
    catalogue = seen['available_queries']
    assert catalogue.count('\n- ') <= routing.SHORTLIST_K


def test_a_near_miss_query_id_is_resolved_onto_the_allowlist():
    route = as_route(SimpleNamespace(tool='sql', query_id='season_presses',
                                     parameters='{}', reasoning='pressing'))
    assert route.query_id == 'season_pressing'


@pytest.mark.parametrize('raw, expected', [
    ('{"season": "2025/2026"}', {'season': '2025/2026'}),
    ("{'season': '2025/2026'}", {'season': '2025/2026'}),
    ('```json\n{"season": "2025/2026"}\n```', {'season': '2025/2026'}),
    ('not json at all', {}),
])
def test_parameters_survive_imperfect_model_output(raw, expected):
    assert as_route(SimpleNamespace(tool='sql', query_id='season_summary',
                                    parameters=raw,
                                    reasoning='')).parameters == expected
