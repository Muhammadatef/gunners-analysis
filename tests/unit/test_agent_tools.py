"""The three tools, and the guarantees they are supposed to provide.

Everything here runs without an LLM. That is the point of the design: the
properties that make the agent trustworthy — no free-form SQL, no unsandboxed
evaluation, no unfiltered retrieval — are properties of the tools, and can be
tested exhaustively and for free. Only the wording is left to the model.
"""

import pytest

from agent.tools import sql_registry
from agent.tools.python_tool import UnsafeExpression, evaluate
from agent.tools.retriever import FILTERABLE, Document, Retriever


# --- the SQL allowlist ------------------------------------------------------

def test_the_registry_is_not_empty():
    assert len(sql_registry.REGISTRY) >= 10


def test_every_query_is_a_select():
    """The one hard guarantee: nothing here can write."""
    for query in sql_registry.REGISTRY.values():
        assert query.sql.strip().upper().startswith('SELECT'), query.id


@pytest.mark.parametrize('forbidden', ['INSERT ', 'UPDATE ', 'DELETE ', 'DROP ',
                                       'ALTER ', 'TRUNCATE', 'GRANT ', 'COPY '])
def test_no_query_contains_a_write_statement(forbidden):
    for query in sql_registry.REGISTRY.values():
        assert forbidden not in query.sql.upper(), f'{query.id} contains {forbidden}'


def test_every_query_declares_the_parameters_its_sql_uses():
    """A mismatch here surfaces as a psycopg2 error at answer time, which is
    the worst moment to find out."""
    import re
    for query in sql_registry.REGISTRY.values():
        used = set(re.findall(r'%\((\w+)\)s', query.sql))
        assert used == set(query.params), (
            f'{query.id}: sql uses {sorted(used)}, declares {sorted(query.params)}')


def test_an_unknown_query_is_refused():
    with pytest.raises(sql_registry.QueryNotAllowed):
        sql_registry.run('definitely_not_a_query', {})


def test_a_sql_string_is_not_a_query_id():
    """The model cannot smuggle SQL in where an id is expected."""
    with pytest.raises(sql_registry.QueryNotAllowed):
        sql_registry.run('SELECT * FROM events', {})


def test_missing_parameters_are_refused_before_execution():
    with pytest.raises(sql_registry.QueryNotAllowed) as exc:
        sql_registry.run('season_summary', {'team': 'Arsenal'})
    assert 'season' in str(exc.value)


def test_the_catalogue_describes_every_query():
    described = sql_registry.describe_all()
    for query_id in sql_registry.REGISTRY:
        assert query_id in described
    assert 'YYYY/YYYY' in described


@pytest.mark.parametrize('raw,expected', [
    ('2025/2026', '2025/2026'),
    ('2025/26', '2025/2026'),
    ('2025-26', '2025/2026'),
    ('2024/25', '2024/2025'),
    ('2025-2026', '2025/2026'),
])
def test_season_shorthand_is_normalised(raw, expected):
    assert sql_registry.normalise_season(raw) == expected


def test_run_normalises_season_before_binding():
    """A mistyped season must not silently return an empty row set."""
    bound = sql_registry.normalise_params({'team': 'Arsenal', 'season': '2025/26'})
    assert bound['season'] == '2025/2026'


def test_near_miss_query_ids_resolve():
    assert sql_registry.resolve_query_id('season_presses') == 'season_pressing'
    assert sql_registry.resolve_query_id('season_pressing') == 'season_pressing'
    assert sql_registry.resolve_query_id('definitely_not_real') is None


def test_season_shorthand_in_prose_is_not_a_hallucination():
    from agent.eval.metrics import hallucinated_numbers
    # "2025/26" must not flag 26.0 as an unsupported figure.
    assert hallucinated_numbers(
        'Arsenal pressed high in 2025/26 with a PPDA of 10.76',
        {10.76}) == set()


# --- provenance -------------------------------------------------------------

def test_evidence_reports_the_numbers_it_vouches_for():
    ev = sql_registry.Evidence(
        query_id='season_summary', params={}, description='d',
        value={'points': 85, 'won': 26},
        rows=[{'points': 85, 'won': 26}])
    assert {85.0, 26.0} <= ev.numbers()


def test_evidence_includes_decimal_values():
    from decimal import Decimal
    ev = sql_registry.Evidence(
        query_id='season_pressing', params={}, description='d',
        value={'avg_ppda': Decimal('10.76'), 'matches': 38},
        rows=[{'avg_ppda': Decimal('10.76'), 'matches': 38}])
    assert {10.76, 38.0} <= ev.numbers()


def test_evidence_finds_numbers_nested_in_rows():
    ev = sql_registry.Evidence(
        query_id='q', params={}, description='d', value=None,
        rows=[{'a': {'b': 12.5}}, {'c': [7]}])
    assert {12.5, 7.0} <= ev.numbers()


def test_evidence_ignores_booleans():
    """True is not the number 1 in any sense an answer should quote."""
    ev = sql_registry.Evidence(query_id='q', params={}, description='d',
                               value={'ok': True}, rows=[])
    assert ev.numbers() == set()


# --- the python sandbox -----------------------------------------------------

def test_arithmetic_works():
    assert evaluate('71 - 65.84').result == pytest.approx(5.16)


def test_functions_are_available():
    assert evaluate('round(100 * 26 / 38)').result == 68


@pytest.mark.parametrize('expression', [
    '__import__("os").system("ls")',
    'open("/etc/passwd").read()',
    '().__class__.__bases__',
    'eval("1+1")',
    'exec("x=1")',
    'globals()',
    '[].__class__',
    'lambda: 1',
])
def test_escape_attempts_are_refused(expression):
    """No names, no attributes, no calls but the whitelisted ones — the AST
    walker never reaches a value for any of these."""
    with pytest.raises(UnsafeExpression):
        evaluate(expression)


def test_a_huge_exponent_is_refused():
    """9**9**9 would occupy the machine rather than raise."""
    with pytest.raises(UnsafeExpression):
        evaluate('9 ** 9999')


def test_division_by_zero_is_a_clean_error():
    with pytest.raises(UnsafeExpression):
        evaluate('1 / 0')


def test_an_empty_expression_is_refused():
    with pytest.raises(UnsafeExpression):
        evaluate('   ')


def test_a_very_long_expression_is_refused():
    with pytest.raises(UnsafeExpression):
        evaluate('1+' * 400 + '1')


def test_a_calculation_carries_its_own_provenance():
    calc = evaluate('71 - 65.84')
    assert '71 - 65.84' in calc.as_prompt()


# --- retrieval --------------------------------------------------------------

def test_the_pgvector_retriever_satisfies_the_protocol():
    """The spec asks for a swappable backend; this is what makes it swappable."""
    from agent.tools.retriever import PgVectorRetriever
    assert isinstance(PgVectorRetriever(schema='gunners'), Retriever)


def test_a_fake_retriever_also_satisfies_it():
    class Fake:
        def search(self, query, k=5, filters=None):
            return []
    assert isinstance(Fake(), Retriever)


def test_only_allowlisted_fields_can_be_filtered():
    """Filters come from model output, so the field names are an allowlist for
    the same reason the queries are."""
    assert 'opponent' in FILTERABLE
    assert 'content' not in FILTERABLE
    assert 'embedding' not in FILTERABLE


def test_a_document_renders_its_own_provenance():
    doc = Document(content='Arsenal pressed high.', doc_type='tactical',
                   score=0.9, opponent='Leeds', match_date='2025-08-23')
    prompt = doc.as_prompt()
    assert 'tactical' in prompt and 'Leeds' in prompt
