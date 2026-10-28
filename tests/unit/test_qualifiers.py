"""`events.qualifiers` has to reach Postgres as structured data.

The panels stringify each event's qualifier list with `str()` so the notebook's
`qualifiers.str.contains(...)` filters keep working. That repr is load-bearing
for rendering and stays — but writing it to a JSONB column stored a JSON
*string*, so `jsonb_typeof` returned 'string', the GIN index could serve no
containment predicate, and no set-piece or pass-type feature could be filtered
in SQL at all. 55,130 rows were affected.

`parse_qualifiers` is the boundary between the two worlds.
"""

import pytest

from db.aggregate import parse_qualifiers

# What the panels actually produce: a Python repr, single quotes and all.
REAL = ("[{'type': {'value': 56, 'displayName': 'Zone'}, 'value': 'Back'}, "
        "{'type': {'value': 212, 'displayName': 'Length'}, 'value': '30.7'}]")


def test_a_python_repr_becomes_real_structure():
    out = parse_qualifiers(REAL)
    assert isinstance(out, list) and len(out) == 2
    assert out[0]['type']['displayName'] == 'Zone'
    assert out[0]['value'] == 'Back'


def test_the_result_is_json_serialisable():
    """Whatever comes back is handed straight to psycopg2's Json wrapper."""
    import json
    json.dumps(parse_qualifiers(REAL))


def test_an_empty_list_survives_as_an_empty_list():
    """An event with no qualifiers is still structured, not missing."""
    assert parse_qualifiers('[]') == []


def test_already_parsed_input_passes_through():
    value = [{'type': {'displayName': 'Cross'}}]
    assert parse_qualifiers(value) is value


@pytest.mark.parametrize('value', [None, '', '   ', 'nan', 'None'])
def test_absent_values_become_none(value):
    """None means SQL NULL at the call site - never a JSON `null`, which would
    read as a third jsonb kind alongside array and string."""
    assert parse_qualifiers(value) is None


@pytest.mark.parametrize('value', [
    '[{unclosed',
    'not a literal at all',
    '{{{',
])
def test_unparseable_input_becomes_none_rather_than_a_lie(value):
    assert parse_qualifiers(value) is None


@pytest.mark.parametrize('value', ['42', "'a string'", '3.14', 'True'])
def test_a_scalar_literal_is_rejected(value):
    """literal_eval would happily return an int here; the column holds lists."""
    assert parse_qualifiers(value) is None


def test_json_style_input_also_works():
    """Double quotes parse too - literal_eval accepts them."""
    out = parse_qualifiers('[{"type": {"displayName": "Cross"}}]')
    assert out == [{'type': {'displayName': 'Cross'}}]


def test_non_string_non_list_input_is_rejected():
    assert parse_qualifiers(42) is None
    assert parse_qualifiers(3.14) is None
