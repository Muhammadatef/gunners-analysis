#!/usr/bin/env python3
"""Apply the analyst-layer DDL (db/schema_agent.sql) to a club's schema.

    python db/apply_agent_schema.py                  # gunners
    python db/apply_agent_schema.py --club barcelona

Separate from `db/apply_schema` because the warehouse DDL and the analyst DDL
have different lifecycles: the warehouse is stable and the analyst layer will
change as the agent grows. Every statement is idempotent, so re-running is a
no-op on an up-to-date schema.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.connection import club_schema, connect, current_schema, schema_sql  # noqa: E402

AGENT_DDL = os.path.join(ROOT, 'db', 'schema_agent.sql')


def apply(schema: str | None = None, path: str = AGENT_DDL) -> list[str]:
    """Run the analyst DDL against `schema`; return the tables that now exist."""
    schema = schema or current_schema()
    with open(path) as fh:
        ddl = fh.read()

    # The file's only schema-qualified line is its search_path header, rewritten
    # here the same way db.connection.schema_sql rewrites the warehouse DDL.
    ddl = ddl.replace('SET search_path TO gunners, public;',
                      f'SET search_path TO {schema}, public;')

    with connect(schema=schema) as conn:
        cur = conn.cursor()
        cur.execute(ddl)
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name IN ('possession_sequences', 'pressure_events',
                                 'carries', 'match_tactical_features',
                                 'semantic_documents')
            ORDER BY table_name
        """, (schema,))
        return [r[0] for r in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--club', default=None)
    ap.add_argument('--file', default=AGENT_DDL)
    a = ap.parse_args(argv)

    schema = club_schema(a.club) if a.club else current_schema()
    tables = apply(schema, a.file)
    print(f'schema {schema}: {len(tables)} analyst table(s) present')
    for t in tables:
        print(f'  {t}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
