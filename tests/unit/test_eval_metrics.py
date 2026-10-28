"""The metrics that decide whether the agent is any good.

These are load-bearing twice over: they score the agent, and they are the
objective an optimizer searches against. A metric that is subtly wrong does not
merely mis-report — it teaches the optimizer to chase the wrong thing.

The number extractor in particular earned its tests. Its first version used
`\\d{1,3}(?:,\\d{3})*` for comma grouping, which matches at most three digits
when there is no comma, so "2025" came apart into "202" and "5" and both were
reported as fabricated figures in an answer that contained neither.
"""

import pytest

from agent.eval.metrics import (RunStats, factual_accuracy, hallucinated_numbers,
                                hallucination_rate, numbers_in, refused,
                                tool_selection_accuracy)


class Ex:
    def __init__(self, **kw):
        self.answer_value = kw.get('answer_value')
        self.answer_type = kw.get('answer_type', 'number')
        self.expected_tool = kw.get('expected_tool', 'sql')
        self.question = kw.get('question', 'q')
        self.query_params = kw.get('query_params', {})


class Pred:
    def __init__(self, answer='', tools_used=None):
        self.answer = answer
        self.tools_used = tools_used if tools_used is not None else []


# --- number extraction ------------------------------------------------------

def test_a_four_digit_year_is_one_number_not_two():
    """The regression: "2025" must not become 202 and 5."""
    assert numbers_in('the 2025 season') == {2025.0}


def test_a_season_string_yields_both_years_whole():
    assert numbers_in('in 2025/2026 they won') == {2025.0, 2026.0}


def test_short_season_shorthand_expands_before_scan():
    assert numbers_in('in 2025/26 they won') == {2025.0, 2026.0}


def test_thousands_separators_are_understood():
    assert 2242.0 in numbers_in('2,242 counterpresses')


def test_a_bare_four_digit_count_survives():
    assert numbers_in('4349 documents') == {4349.0}


def test_decimals_are_kept():
    assert numbers_in('PPDA of 10.76') == {10.76}


def test_negatives_are_kept():
    assert -3.0 in numbers_in('a goal difference of -3')


def test_prose_without_numbers_is_empty():
    assert numbers_in('they pressed high and won the ball back') == set()


# --- hallucination ----------------------------------------------------------

def test_a_number_from_the_evidence_is_not_a_hallucination():
    assert hallucinated_numbers('Arsenal finished on 85 points', {85.0}) == set()


def test_a_number_absent_from_the_evidence_is_flagged():
    assert hallucinated_numbers('Arsenal finished on 91 points', {85.0}) == {91.0}


def test_rounding_is_not_lying():
    """10.8 for 10.76 is a reasonable rendering, not a fabrication."""
    assert hallucinated_numbers('a PPDA of 10.8', {10.76}) == set()


def test_years_in_prose_are_not_treated_as_claims():
    assert hallucinated_numbers('in the 2025/2026 season they scored 71', {71.0}) == set()


def test_the_rate_is_the_share_of_unsupported_figures():
    # 85 supported, 91 and 100 not -> 2 of 3
    rate = hallucination_rate('85 points, 91 goals, 100 shots', {85.0})
    assert rate == pytest.approx(2 / 3)


def test_an_answer_with_no_numbers_cannot_hallucinate_one():
    assert hallucination_rate('They pressed aggressively.', set()) == 0.0


# --- factual accuracy -------------------------------------------------------

def test_a_correct_number_scores_one():
    assert factual_accuracy(Ex(answer_value=85), Pred('They finished on 85 points')) == 1.0


def test_a_wrong_number_scores_zero():
    assert factual_accuracy(Ex(answer_value=85), Pred('They finished on 80 points')) == 0.0


def test_wording_does_not_matter_only_the_value():
    assert factual_accuracy(Ex(answer_value=85), Pred('85')) == 1.0


def test_a_list_answer_needs_every_element():
    ex = Ex(answer_value=[26, 7, 5], answer_type='list')
    assert factual_accuracy(ex, Pred('26 wins, 7 draws, 5 losses')) == 1.0
    assert factual_accuracy(ex, Pred('26 wins, 7 draws')) == 0.0


def test_a_refusal_question_is_scored_on_refusing():
    ex = Ex(answer_value='refuse', answer_type='refusal')
    assert factual_accuracy(ex, Pred('That cannot be answered from this data.')) == 1.0
    assert factual_accuracy(ex, Pred('They paid about £60 million.')) == 0.0


@pytest.mark.parametrize('text', [
    'This cannot be answered from the available evidence.',
    'The warehouse holds no data on injuries.',
    'I do not have that information.',
    'There is no evidence for this in the dataset.',
])
def test_refusals_are_recognised(text):
    assert refused(text) is True


def test_a_confident_answer_is_not_a_refusal():
    assert refused('Arsenal finished on 85 points.') is False


# --- tool selection ---------------------------------------------------------

def test_using_the_expected_tool_scores_one():
    assert tool_selection_accuracy(Ex(expected_tool='sql'), Pred(tools_used=['sql'])) == 1.0


def test_using_the_wrong_tool_scores_zero():
    assert tool_selection_accuracy(Ex(expected_tool='sql'),
                                   Pred(tools_used=['retrieval'])) == 0.0


def test_a_question_that_should_be_refused_must_touch_no_tool():
    ex = Ex(expected_tool='none')
    assert tool_selection_accuracy(ex, Pred(tools_used=[])) == 1.0
    assert tool_selection_accuracy(ex, Pred(tools_used=['sql'])) == 0.0


# --- latency ----------------------------------------------------------------

def test_p95_uses_nearest_rank_not_interpolation():
    stats = RunStats()
    for i in range(1, 21):          # 1..20 seconds
        stats.record(float(i))
    assert stats.p95_latency == 19.0


def test_p95_of_a_single_sample_is_that_sample():
    stats = RunStats()
    stats.record(4.2)
    assert stats.p95_latency == 4.2


def test_cost_accumulates():
    stats = RunStats()
    stats.record(1.0, 0.01)
    stats.record(1.0, 0.03)
    assert stats.total_cost == pytest.approx(0.04)
    assert stats.cost_per_query == pytest.approx(0.02)
