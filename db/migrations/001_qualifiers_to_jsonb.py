#!/usr/bin/env python3
"""Convert `events.qualifiers` from a stringified Python repr to real JSONB.

    python db/migrations/001_qualifiers_to_jsonb.py --dry-run
    python db/migrations/001_qualifiers_to_jsonb.py

The column has always been declared `JSONB` and has always carried a GIN index,
but the value written was a JSON *string* holding a Python repr:

    "[{'type': {'value': 56, 'displayName': 'Zone'}, 'value': 'Back'}]"

so `jsonb_typeof` returned `'string'`, the index could serve no `@>` or `?`
predicate, and every qualifier query in the codebase had to fall back to
substring matching. Nothing that reads a qualifier could be done in SQL.

`db/aggregate.parse_qualifiers` fixes the write path. This fixes the rows
already in the warehouse. Both are needed: the write path alone would leave the
existing season stringified, and the backfill alone would be undone by the next
ingest.

Safe to re-run: rows already stored as an array are skipped, so a partial run
resumes cleanly. Converts in batches inside one transaction per batch, so an
interruption leaves the table consistent rather than half-typed.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg2.extras import Json, execute_values  # noqa: E402

from db.aggregate import parse_qualifiers  # noqa: E402
from db.connection import connect, current_schema  # noqa: E402


def survey(cur) -> dict[str, int]:
    cur.execute("""
        SELECT COALESCE(jsonb_typeof(qualifiers), 'sql-null') AS kind, count(*)
        FROM events GROUP BY 1 ORDER BY 1
    """)
    return {kind: n for kind, n in cur.fetchall()}


def convert(schema: str, batch: int = 5000, dry_run: bool = False) -> dict:
    stats = {'converted': 0, 'already_array': 0, 'unparseable': 0, 'emptied': 0}

    with connect(schema=schema) as conn:
        cur = conn.cursor()
        before = survey(cur)
        print('before:', before)

        stats['already_array'] = before.get('array', 0) + before.get('object', 0)
        todo = before.get('string', 0)
        if not todo:
            print('nothing to convert - no stringified qualifiers remain')
            return stats | {'after': before}

        print(f'converting {todo:,} stringified rows in batches of {batch:,}')
        while True:
            cur.execute("""
                SELECT event_uid, qualifiers #>> '{}'
                FROM events
                WHERE jsonb_typeof(qualifiers) = 'string'
                LIMIT %s
            """, (batch,))
            rows = cur.fetchall()
            if not rows:
                break

            updates = []
            for uid, text in rows:
                parsed = parse_qualifiers(text)
                if parsed is None:
                    stats['unparseable' if text else 'emptied'] += 1
                    updates.append((uid, None))
                else:
                    stats['converted'] += 1
                    updates.append((uid, Json(parsed)))

            if dry_run:
                print(f'  dry run: would update {len(updates):,} rows; stopping')
                break

            execute_values(cur, """
                UPDATE events SET qualifiers = data.q
                FROM (VALUES %s) AS data (uid, q)
                WHERE events.event_uid = data.uid
            """, updates, template='(%s, %s::jsonb)')
            conn.commit()
            print(f'  {stats["converted"]:,} converted ...', flush=True)

        after = survey(cur)
        print('after:', after)
        stats['after'] = after

    return stats


def verify(schema: str) -> bool:
    """The index is only worth having if the planner will actually use it."""
    with connect(schema=schema) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) FROM events
            WHERE qualifiers @> '[{"type": {"displayName": "Cross"}}]'
        """)
        crosses = cur.fetchone()[0]

        cur.execute('SET LOCAL enable_seqscan = off')
        cur.execute("""
            EXPLAIN (FORMAT JSON)
            SELECT count(*) FROM events
            WHERE qualifiers @> '[{"type": {"displayName": "Cross"}}]'
        """)
        plan = str(cur.fetchone()[0])

    used_gin = 'idx_events_qualifiers' in plan
    print(f'\ncrosses found via @> containment : {crosses:,}')
    print(f'GIN index appears in the plan    : {used_gin}')
    return crosses > 0 and used_gin


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--schema', default=None)
    ap.add_argument('--batch', type=int, default=5000)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)

    schema = a.schema or current_schema()
    print(f'schema: {schema}')

    stats = convert(schema, batch=a.batch, dry_run=a.dry_run)
    print(f"\nconverted    {stats['converted']:,}")
    print(f"already ok   {stats['already_array']:,}")
    print(f"emptied      {stats['emptied']:,}")
    print(f"unparseable  {stats['unparseable']:,}")

    if a.dry_run:
        return 0

    after = stats.get('after', {})
    if after.get('string'):
        print(f"\n! {after['string']:,} rows are still strings", file=sys.stderr)
        return 1
    return 0 if verify(schema) else 1


if __name__ == '__main__':
    sys.exit(main())
