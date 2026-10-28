"""Schema-per-club: name validation and DDL retargeting.

One club, one schema. `gunners` is Arsenal's; everything else gets a clone of
db/schema.sql. None of this touches Postgres - it is all string work plus the
guard that stops a club's tables being built inside `gunners` by accident.
"""

import os

import pytest

from db import connection

SCHEMA_SQL = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'db', 'schema.sql')


@pytest.fixture(scope='module')
def ddl():
    with open(SCHEMA_SQL) as fh:
        return fh.read()


# --- club name -> schema name ----------------------------------------------

@pytest.mark.parametrize('club,expected', [
    ('barcelona', 'barcelona'),
    ('Barcelona', 'barcelona'),
    ('AC Milan', 'ac_milan'),
    ('Al Ahly SC', 'al_ahly_sc'),
    ('Bayern München', 'bayern_munchen'),
    ('Brighton & Hove Albion', 'brighton_hove_albion'),
])
def test_normalise_club(club, expected):
    assert connection.normalise_club(club) == expected


@pytest.mark.parametrize('name', ['gunners', 'barcelona', 'ac_milan', 'a', 'club2'])
def test_validate_schema_accepts(name):
    assert connection.validate_schema(name) == name


@pytest.mark.parametrize('name', [
    'public',                  # would collide with the shared schema
    'pg_temp',                 # reserved
    'pg_catalog',
    'information_schema',
    'Barcelona',               # uppercase would need quoting
    'Barça',                   # non-ascii
    '2026',                    # must start with a letter
    '',
    None,
    "a'; DROP SCHEMA gunners CASCADE; --",
    'x' * 64,                  # past the 63-byte identifier limit
])
def test_validate_schema_rejects(name):
    with pytest.raises(ValueError):
        connection.validate_schema(name)


def test_club_schema_normalises_then_validates():
    assert connection.club_schema('AC Milan') == 'ac_milan'
    with pytest.raises(ValueError):
        connection.club_schema('public')


# --- retargeting db/schema.sql ---------------------------------------------

def test_schema_sql_rewrites_only_the_header(ddl):
    out = connection.schema_sql(ddl, 'barcelona')

    assert 'CREATE SCHEMA IF NOT EXISTS barcelona;' in out
    assert 'SET search_path TO barcelona, public;' in out
    assert 'CREATE SCHEMA IF NOT EXISTS gunners;' not in out
    assert 'SET search_path TO gunners, public;' not in out

    # Everything else is unqualified and must come through untouched.
    for kind in ('CREATE TABLE', 'CREATE INDEX', 'CREATE VIEW', 'REFERENCES'):
        assert out.count(kind) == ddl.count(kind), kind
    assert len(out.splitlines()) == len(ddl.splitlines())


def test_schema_sql_validates_the_target(ddl):
    with pytest.raises(ValueError):
        connection.schema_sql(ddl, 'public')


def test_schema_sql_raises_when_the_header_drifts():
    """The guard that matters.

    Without it, a schema.sql whose header stopped matching would be executed
    verbatim - silently building a club's tables inside `gunners`.
    """
    with pytest.raises(RuntimeError, match='schema.sql'):
        connection.schema_sql('CREATE TABLE seasons (season_id SERIAL);', 'barcelona')


def test_schema_sql_is_idempotent(ddl):
    once = connection.schema_sql(ddl, 'barcelona')
    assert connection.schema_sql(once, 'barcelona') == once


# --- which schema a connection lands in ------------------------------------

def test_default_schema_is_gunners(monkeypatch):
    monkeypatch.delenv('GUNNERS_DB_SCHEMA', raising=False)
    assert connection.current_schema() == 'gunners'


def test_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv('GUNNERS_DB_SCHEMA', 'barcelona')
    assert connection.current_schema() == 'barcelona'


def test_contextvar_beats_env(monkeypatch):
    monkeypatch.setenv('GUNNERS_DB_SCHEMA', 'barcelona')
    with connection.use_schema('ac_milan'):
        assert connection.current_schema() == 'ac_milan'
    assert connection.current_schema() == 'barcelona'


def test_use_schema_resets_on_exit(monkeypatch):
    monkeypatch.delenv('GUNNERS_DB_SCHEMA', raising=False)
    with connection.use_schema('barcelona'):
        with connection.use_schema('ac_milan'):
            assert connection.current_schema() == 'ac_milan'
        assert connection.current_schema() == 'barcelona'
    assert connection.current_schema() == 'gunners'


def test_use_schema_resets_after_an_exception(monkeypatch):
    monkeypatch.delenv('GUNNERS_DB_SCHEMA', raising=False)
    with pytest.raises(RuntimeError):
        with connection.use_schema('barcelona'):
            raise RuntimeError('boom')
    assert connection.current_schema() == 'gunners'


def test_use_schema_none_is_a_noop():
    with connection.use_schema(None):
        assert connection.current_schema() == 'gunners'


def test_use_schema_validates():
    with pytest.raises(ValueError):
        with connection.use_schema('pg_temp'):
            pass
