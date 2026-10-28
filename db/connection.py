"""Postgres connection helpers for the GunnersAnalysis warehouse.

One club, one schema. Arsenal lives in `gunners`; every other club we analyse
gets its own clone of db/schema.sql (`barcelona`, `ac_milan`, `al_ahly`, ...).
Nothing in the DDL or in any query is schema-qualified - the whole warehouse
resolves through `search_path`, so pointing a run at a different club is a
matter of setting one session variable.

Which schema a connection lands in, highest precedence first:

    connect(schema='barcelona')      explicit, used by the one true writer
    use_schema('barcelona')          contextvar, set once at an entry point
    GUNNERS_DB_SCHEMA=barcelona      environment
    'gunners'                        default, so every existing caller is unchanged
"""

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

DEFAULT_DSN = 'postgresql://gunners:gunners@postgres:5432/gunners'
DEFAULT_SCHEMA = 'gunners'

# Postgres identifiers are 63 bytes; keep to lowercase ascii so nothing ever
# needs quoting outside the psycopg2.sql.Identifier we already use.
_SAFE_SCHEMA_RE = re.compile(r'^[a-z][a-z0-9_]{0,62}$')
_RESERVED_SCHEMAS = {'public', 'information_schema', 'pg_catalog', 'pg_toast', 'pg_temp'}

# A contextvar rather than a module global or an env var: FastAPI background
# tasks run through anyio.to_thread.run_sync, which copies the context, so a
# Barcelona job and an Arsenal job can be in flight in one process without
# clobbering each other. Note the flip side - a bare threading.Thread starts
# with a *fresh* context and would fall back to `gunners`, which is why
# db.loader.load passes schema= explicitly rather than relying on ambient state.
_schema_var: ContextVar[str | None] = ContextVar('gunners_db_schema', default=None)


def dsn() -> str:
    return os.getenv('GUNNERS_DB_URL', DEFAULT_DSN)


# ---------------------------------------------------------------------------
# Club -> schema name
# ---------------------------------------------------------------------------

def normalise_club(club: str) -> str:
    """'AC Milan' -> 'ac_milan', 'Al Ahly SC' -> 'al_ahly_sc'."""
    import unicodedata
    value = unicodedata.normalize('NFKD', str(club))
    value = value.encode('ascii', 'ignore').decode('ascii').lower()
    value = re.sub(r'[^a-z0-9]+', '_', value).strip('_')
    return value


def validate_schema(name: str) -> str:
    """Return `name` if it is safe to use as a schema, else raise ValueError."""
    if not _SAFE_SCHEMA_RE.match(name or ''):
        raise ValueError(
            f'invalid schema name {name!r}: expected lowercase letters, digits and '
            'underscores, starting with a letter (try --club barcelona)')
    if name in _RESERVED_SCHEMAS or name.startswith('pg_'):
        raise ValueError(f'{name!r} is a reserved Postgres schema')
    return name


def club_schema(club: str) -> str:
    """normalise + validate in one step. The entry points call this."""
    return validate_schema(normalise_club(club))


def current_schema() -> str:
    return _schema_var.get() or os.getenv('GUNNERS_DB_SCHEMA') or DEFAULT_SCHEMA


@contextmanager
def use_schema(schema: str | None):
    """Route every connect()/cursor() in this context at `schema`."""
    if schema is None:
        yield current_schema()
        return
    token = _schema_var.set(validate_schema(schema))
    try:
        yield schema
    finally:
        _schema_var.reset(token)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@contextmanager
def connect(autocommit: bool = False, schema: str | None = None):
    """Yield a connection with search_path already set to the club schema."""
    target = validate_schema(schema) if schema else current_schema()
    conn = psycopg2.connect(dsn())
    conn.autocommit = autocommit
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL('SET search_path TO {}, public').format(
                sql.Identifier(target)))
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def cursor(dict_rows: bool = False, schema: str | None = None):
    with connect(schema=schema) as conn:
        factory = RealDictCursor if dict_rows else None
        with conn.cursor(cursor_factory=factory) as cur:
            yield cur


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

# The only two lines in db/schema.sql that name a schema. Everything else in
# the file is unqualified, so retargeting the whole 500-line DDL is a matter of
# rewriting this header.
_SCHEMA_HEADER_RE = re.compile(
    r'^CREATE SCHEMA IF NOT EXISTS\s+\w+\s*;[ \t]*\n'
    r'SET search_path TO\s+\w+\s*,\s*public\s*;',
    re.MULTILINE)


def schema_sql(ddl: str, schema: str) -> str:
    """Retarget db/schema.sql at `schema`, in memory.

    Never rewrite the file on disk: docker-compose bind-mounts it into
    /docker-entrypoint-initdb.d, where it has to stay a valid standalone
    `gunners` bootstrap.
    """
    validate_schema(schema)
    replacement = (f'CREATE SCHEMA IF NOT EXISTS {schema};\n'
                   f'SET search_path TO {schema}, public;')
    out, count = _SCHEMA_HEADER_RE.subn(replacement, ddl, count=1)
    if count != 1:
        # Without this the DDL would silently run against whatever header the
        # file does carry - i.e. build a club's tables inside `gunners`.
        raise RuntimeError(
            'db/schema.sql no longer starts with the expected '
            '"CREATE SCHEMA ...; SET search_path TO ..., public;" header; '
            'db.connection.schema_sql needs updating before it can retarget it')
    return out


def _default_schema_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')


def apply_schema(path: str | None = None, schema: str | None = None) -> str:
    """Run db/schema.sql against `schema`. Safe to call repeatedly."""
    target = validate_schema(schema) if schema else current_schema()
    with open(path or _default_schema_path()) as fh:
        ddl = fh.read()
    if target != DEFAULT_SCHEMA:
        ddl = schema_sql(ddl, target)
    with connect(schema=target) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    return target


def schema_exists(schema: str) -> bool:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM information_schema.schemata WHERE schema_name = %s',
                        (validate_schema(schema),))
            return cur.fetchone() is not None


def ensure_schema(schema: str, path: str | None = None) -> str:
    """Create the club schema and its tables if they are not there yet."""
    target = validate_schema(schema)
    if not schema_exists(target):
        apply_schema(path, schema=target)
    return target


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='Apply db/schema.sql to a club schema')
    ap.add_argument('--schema', default=None, help='target schema (default: gunners)')
    ap.add_argument('--club', default=None,
                    help="club name, slugified into a schema ('AC Milan' -> ac_milan)")
    ap.add_argument('--file', default=None, help='path to schema.sql')
    a = ap.parse_args()

    target = a.schema or (club_schema(a.club) if a.club else None)
    applied = apply_schema(a.file, schema=target)
    print(f'schema {applied} applied to {dsn().rsplit("@", 1)[-1]}')
