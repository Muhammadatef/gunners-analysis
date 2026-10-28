#!/usr/bin/env python3
"""Data readiness assessment for the season warehouse.

Answers one question: is there enough history in Postgres to build a football
analyst on, or only enough to describe a single afternoon?

    python db/readiness.py                       # 2025/2026, gunners schema
    python db/readiness.py --season '2026/2027'
    python db/readiness.py --club barcelona --json

Three sections, in order of how much they matter:

  COVERAGE    how many of the season's matches actually landed
  DIMENSIONS  which of the tactical dimensions an analyst asks about exist,
              per match, and where they stop
  GATES       the seven go/no-go conditions from the readiness checklist

Exits 0 when every gate passes, 1 otherwise, so a DAG or a CI job can block on
it. Nothing here writes; it is safe to run against production at any time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.connection import club_schema, cursor  # noqa: E402

# A Premier League season is 38 matches per club. Nothing else in the codebase
# should have to know this number.
PL_SEASON_MATCHES = 38

# The WhoScored/Opta event `type` values that stand in for each dimension.
# Opta has no pressure or carry event, which is exactly why those two rows
# below resolve against derived tables instead.
EVENT_FAMILIES = {
    'passes':            ('Pass',),
    'shots':             ('Goal', 'SavedShot', 'MissedShots', 'ShotOnPost'),
    'substitutions':     ('SubstitutionOn', 'SubstitutionOff'),
    'recoveries':        ('BallRecovery',),
    'defensive_actions': ('Tackle', 'Interception', 'Clearance', 'BlockedPass',
                          'Challenge', 'BallRecovery'),
    'formations':        ('FormationSet', 'FormationChange'),
}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(current_schema() || '.' || %s) IS NOT NULL", (name,))
    return bool(cur.fetchone()[0])


def _scalar(cur, sql: str, params=()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else 0


def _season(cur, name: str) -> dict | None:
    cur.execute('SELECT season_id, name, competition, start_date, end_date '
                'FROM seasons WHERE name = %s', (name,))
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip(('season_id', 'name', 'competition', 'start_date', 'end_date'), row))


def coverage(cur, season_id: int | None) -> dict:
    """Calendar vs reality: fixtures seeded, matches ingested, gaps."""
    if season_id is None:
        return {'fixtures': 0, 'fixtures_expected': PL_SEASON_MATCHES, 'matches': 0,
                'ingested': 0, 'duplicate_matchweeks': 0, 'opponents': 0,
                'matches_out_of_season': 0}

    dup = _scalar(cur, """
        SELECT COALESCE(SUM(n - 1), 0) FROM (
            SELECT COUNT(*) n FROM fixtures WHERE season_id = %s
            GROUP BY matchweek, scheduled_date, opponent, venue HAVING COUNT(*) > 1
        ) d
    """, (season_id,))

    # A match whose date falls outside the season window is a mis-linked
    # ingest - the Crystal Palace case that started this assessment.
    stray = _scalar(cur, """
        SELECT COUNT(*) FROM matches m JOIN seasons s USING (season_id)
        WHERE m.season_id = %s
          AND s.start_date IS NOT NULL
          AND (m.match_date < s.start_date OR m.match_date > s.end_date)
    """, (season_id,))

    return {
        'fixtures': _scalar(cur, 'SELECT COUNT(*) FROM fixtures WHERE season_id = %s',
                            (season_id,)),
        'fixtures_expected': PL_SEASON_MATCHES,
        'duplicate_matchweeks': dup,
        'matches': _scalar(cur, 'SELECT COUNT(*) FROM matches WHERE season_id = %s',
                           (season_id,)),
        'ingested': _scalar(cur, "SELECT COUNT(*) FROM fixtures "
                                 "WHERE season_id = %s AND status = 'ingested'",
                            (season_id,)),
        'opponents': _scalar(cur, """
            SELECT COUNT(DISTINCT s.opponent_team_id)
            FROM match_team_stats s JOIN matches m USING (match_id)
            WHERE m.season_id = %s AND s.team_id <> s.opponent_team_id
        """, (season_id,)),
        'matches_out_of_season': stray,
    }


def dimensions(cur, season_id: int | None, total_matches: int) -> list[dict]:
    """Per-dimension: how many of the ingested matches carry it at all.

    `matches` is the count with at least one row; `rows` the raw volume. A
    dimension backed by a table that does not exist yet reports present=False
    with a note, which is the signal to run the tactical migration.
    """
    out: list[dict] = []

    def event_dim(label: str, types: tuple[str, ...], note: str = ''):
        n_matches = _scalar(cur, """
            SELECT COUNT(DISTINCT e.match_id) FROM events e JOIN matches m USING (match_id)
            WHERE m.season_id = %s AND e.type = ANY(%s)
        """, (season_id, list(types)))
        rows = _scalar(cur, """
            SELECT COUNT(*) FROM events e JOIN matches m USING (match_id)
            WHERE m.season_id = %s AND e.type = ANY(%s)
        """, (season_id, list(types)))
        out.append({'dimension': label, 'source': 'events', 'matches': n_matches,
                    'rows': rows, 'complete': n_matches == total_matches and total_matches > 0,
                    'note': note})

    def table_dim(label: str, table: str, where: str = '', note: str = ''):
        if not _table_exists(cur, table):
            out.append({'dimension': label, 'source': table, 'matches': 0, 'rows': 0,
                        'complete': False, 'note': note or f'table {table} does not exist'})
            return
        clause = f' AND {where}' if where else ''
        n_matches = _scalar(cur, f"""
            SELECT COUNT(DISTINCT t.match_id) FROM {table} t JOIN matches m USING (match_id)
            WHERE m.season_id = %s{clause}
        """, (season_id,))
        rows = _scalar(cur, f"""
            SELECT COUNT(*) FROM {table} t JOIN matches m USING (match_id)
            WHERE m.season_id = %s{clause}
        """, (season_id,))
        out.append({'dimension': label, 'source': table, 'matches': n_matches, 'rows': rows,
                    'complete': n_matches == total_matches and total_matches > 0,
                    'note': note})

    if season_id is None:
        return [{'dimension': d, 'source': '-', 'matches': 0, 'rows': 0, 'complete': False,
                 'note': 'season not in the warehouse'}
                for d in ('opponents', 'lineups', 'formations', 'player_positions',
                          'substitutions', 'shots', 'xg_shot_level', 'possession_chains',
                          'defensive_actions', 'pressing_events', 'passes', 'carries',
                          'recoveries')]

    # -- straight from the feed ---------------------------------------------
    table_dim('opponents', 'match_team_stats')
    table_dim('lineups', 'match_player_stats', 'is_first_eleven IS NOT NULL')
    event_dim('formations', EVENT_FAMILIES['formations'],
              'qualifier blob only - no parsed formation table')
    table_dim('player_positions', 'match_player_stats', "position IS NOT NULL AND position <> ''")
    event_dim('substitutions', EVENT_FAMILIES['substitutions'])
    table_dim('shots', 'shots')
    table_dim('xg_shot_level', 'shots', 't.xg IS NOT NULL',
              'match-level xG comes from SofaScore; per-shot xG needs a model')
    event_dim('passes', EVENT_FAMILIES['passes'])
    event_dim('defensive_actions', EVENT_FAMILIES['defensive_actions'])
    event_dim('recoveries', EVENT_FAMILIES['recoveries'])

    # -- derived, and therefore absent until the tactical ETL runs ----------
    table_dim('carries', 'carries',
              note='Opta emits no carry event; must be derived from consecutive touches')
    table_dim('possession_chains', 'possession_sequences',
              note='must be derived by segmenting the event stream on turnovers')
    table_dim('pressing_events', 'pressure_events',
              note='Opta emits no pressure event; derive PPDA + counterpress windows')

    return out


def qualifier_health(cur, season_id: int | None) -> dict:
    """Is events.qualifiers queryable JSON, or a stringified Python repr?

    The GIN index on that column is worthless if the value is a scalar string,
    and every set-piece / pass-type feature has to parse text at query time.
    """
    if season_id is None or not _table_exists(cur, 'events'):
        return {'total': 0, 'structured': 0, 'stringified': 0, 'healthy': False}
    cur.execute("""
        SELECT jsonb_typeof(e.qualifiers) AS kind, COUNT(*)
        FROM events e JOIN matches m USING (match_id)
        WHERE m.season_id = %s AND e.qualifiers IS NOT NULL
        GROUP BY 1
    """, (season_id,))
    kinds = dict(cur.fetchall())
    total = sum(kinds.values())
    structured = kinds.get('array', 0) + kinds.get('object', 0)
    return {'total': total, 'structured': structured,
            'stringified': kinds.get('string', 0),
            'healthy': total > 0 and structured == total}


def gates(cur, cov: dict, dims: list[dict], quals: dict) -> list[dict]:
    """The seven readiness gates. All must pass before an agent is built."""
    by_name = {d['dimension']: d for d in dims}
    complete = cov['matches'] >= PL_SEASON_MATCHES

    def dim_ok(*names) -> bool:
        return all(by_name.get(n, {}).get('complete') for n in names)

    semantic = _table_exists(cur, 'semantic_documents')
    n_docs = _scalar(cur, 'SELECT COUNT(*) FROM semantic_documents') if semantic else 0
    # Retrieval is pgvector, not ColBERTv2 -- the master prompt was rewritten
    # on the grounds that SQL is the anti-hallucination guarantee and retrieval
    # only supplies soft context, so heavy infrastructure does not belong on
    # the least load-bearing component. The index to check is therefore the
    # HNSW index over semantic_documents.embedding, and "populated" means every
    # document actually has a vector: a corpus that is half-embedded retrieves
    # silently badly, which is worse than one that is empty.
    n_embedded = _scalar(cur, 'SELECT COUNT(embedding) FROM semantic_documents') \
        if semantic else 0
    has_hnsw = bool(_scalar(cur, """
        SELECT COUNT(*) FROM pg_indexes
        WHERE schemaname = current_schema() AND indexname = 'idx_docs_embedding'
    """)) if semantic else False
    features = _table_exists(cur, 'match_tactical_features')
    n_feat = _scalar(cur, 'SELECT COUNT(*) FROM match_tactical_features') if features else 0

    return [
        {'gate': 'Full Premier League season',
         'passed': cov['fixtures'] >= PL_SEASON_MATCHES and cov['duplicate_matchweeks'] == 0,
         'detail': f"{cov['fixtures']} fixtures seeded "
                   f"(expected {PL_SEASON_MATCHES}, {cov['duplicate_matchweeks']} duplicates)"},
        {'gate': 'All Arsenal matches ingested',
         'passed': complete and cov['matches_out_of_season'] == 0,
         'detail': f"{cov['matches']}/{PL_SEASON_MATCHES} matches, "
                   f"{cov['opponents']} distinct opponents, "
                   f"{cov['matches_out_of_season']} dated outside the season window"},
        {'gate': 'Event-level data on every match',
         'passed': complete and dim_ok('passes', 'shots', 'defensive_actions',
                                       'recoveries', 'substitutions') and quals['healthy'],
         'detail': f"passes on {by_name['passes']['matches']}/{cov['matches']} matches; "
                   f"qualifiers structured: {quals['structured']}/{quals['total']}"},
        {'gate': 'Player-level data',
         'passed': complete and dim_ok('lineups', 'player_positions'),
         'detail': f"lineups on {by_name['lineups']['matches']}/{cov['matches']}, "
                   f"positions on {by_name['player_positions']['matches']}/{cov['matches']}"},
        {'gate': 'Tactical features generated',
         'passed': features and n_feat >= PL_SEASON_MATCHES
                   and dim_ok('carries', 'possession_chains', 'pressing_events'),
         'detail': f'{n_feat} feature rows; carries/chains/pressing '
                   + ('present' if dim_ok('carries', 'possession_chains',
                                          'pressing_events') else 'missing')},
        {'gate': 'Semantic documents created',
         'passed': semantic and n_docs > 0,
         'detail': f'{n_docs} documents' if semantic
                   else 'table semantic_documents does not exist'},
        {'gate': 'Vector index populated',
         'passed': semantic and has_hnsw and n_docs > 0 and n_embedded == n_docs,
         'detail': (f'{n_embedded}/{n_docs} documents embedded, HNSW index '
                    f'{"present" if has_hnsw else "MISSING"}') if semantic
                   else 'table semantic_documents does not exist'},
    ]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def assess(season_name: str, schema: str) -> dict:
    with cursor(schema=schema) as cur:
        season = _season(cur, season_name)
        season_id = season['season_id'] if season else None
        cov = coverage(cur, season_id)
        dims = dimensions(cur, season_id, cov['matches'])
        quals = qualifier_health(cur, season_id)
        gts = gates(cur, cov, dims, quals)
        cur.execute('SELECT name FROM seasons ORDER BY start_date')
        available = [r[0] for r in cur.fetchall()]

    return {'schema': schema, 'season': season_name, 'season_found': season is not None,
            'seasons_available': available, 'coverage': cov, 'dimensions': dims,
            'qualifiers': quals, 'gates': gts,
            'ready': all(g['passed'] for g in gts)}


def render(r: dict) -> str:
    tick = lambda ok: '✓' if ok else '✗'  # noqa: E731
    cov, L = r['coverage'], []

    L.append(f"DATA READINESS  -  schema {r['schema']}  season {r['season']}")
    L.append('=' * 72)
    L.append(f"seasons in warehouse : {', '.join(r['seasons_available']) or 'none'}")
    if not r['season_found']:
        L.append(f"  ! {r['season']} is not in the warehouse at all")

    L.append('')
    L.append('COVERAGE')
    L.append(f"  fixtures seeded    {cov['fixtures']:>4} / {cov['fixtures_expected']}"
             + (f"   ({cov['duplicate_matchweeks']} duplicate rows)"
                if cov['duplicate_matchweeks'] else ''))
    L.append(f"  matches ingested   {cov['matches']:>4} / {PL_SEASON_MATCHES}")
    L.append(f"  distinct opponents {cov['opponents']:>4} / 19")
    if cov['matches_out_of_season']:
        L.append(f"  ! {cov['matches_out_of_season']} match(es) dated outside the "
                 f"season window - wrong season linkage")

    L.append('')
    L.append('DIMENSIONS' + ' ' * 12 + 'MATCHES     ROWS  SOURCE')
    for d in r['dimensions']:
        L.append(f"  {tick(d['complete'])} {d['dimension']:<20}"
                 f"{d['matches']:>3}/{cov['matches'] or 0:<3}{d['rows']:>9}  {d['source']}")
        if d['note']:
            L.append(f"      {d['note']}")

    q = r['qualifiers']
    if q['total'] and not q['healthy']:
        L.append('')
        L.append(f"  ! events.qualifiers: {q['stringified']}/{q['total']} rows are "
                 f"stringified Python reprs, not JSON.")
        L.append('    The GIN index cannot be used and no qualifier can be queried in SQL.')

    L.append('')
    L.append('READINESS GATES')
    for g in r['gates']:
        L.append(f"  {tick(g['passed'])} {g['gate']:<34}{g['detail']}")

    L.append('')
    L.append('=' * 72)
    L.append('VERDICT: READY - the dataset supports a season-level analyst' if r['ready']
             else 'VERDICT: NOT READY - do not build the agent yet; backfill first')
    return '\n'.join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--season', default=os.getenv('GUNNERS_SEASON', '2025/2026'))
    ap.add_argument('--club', default='gunners', help='club schema to assess')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    a = ap.parse_args(argv)

    report = assess(a.season, club_schema(a.club))
    print(json.dumps(report, indent=2, default=str) if a.json else render(report))
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
