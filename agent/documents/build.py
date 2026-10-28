#!/usr/bin/env python3
"""Generate, embed and store the semantic document corpus.

    python -m agent.documents.build --season '2025/2026'
    python -m agent.documents.build --match 1903128 --no-embed
    python -m agent.documents.build --season '2025/2026' --rebuild

Reads the warehouse, writes `semantic_documents`. Idempotent per match: the
match's documents are deleted and regenerated, so re-running after improving
the wording replaces rather than duplicates. `--rebuild` clears the whole
corpus first.

Every document is a derived artefact. If this table were dropped, nothing of
value is lost — which is the property that makes it safe to regenerate the
whole corpus whenever the generators change.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg2.extras import RealDictCursor, execute_values  # noqa: E402

from agent.documents import generators as gen  # noqa: E402
from db.connection import club_schema, connect, current_schema  # noqa: E402

# --------------------------------------------------------------------------
# the queries behind each document type
# --------------------------------------------------------------------------

MATCH_SQL = """
    SELECT m.match_id, m.match_date, m.competition, m.matchweek, m.season_id,
           m.home_score, m.away_score, m.ht_home_score, m.ht_away_score,
           m.home_xg, m.away_xg, m.home_xgot, m.away_xgot,
           m.venue_name, m.referee, m.attendance,
           m.home_team_id, m.away_team_id,
           h.name AS home_team_name, a.name AS away_team_name,
           hs.possession_pct AS home_possession, as_.possession_pct AS away_possession,
           hs.shots AS home_shots, as_.shots AS away_shots
    FROM matches m
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    LEFT JOIN match_team_stats hs ON hs.match_id = m.match_id AND hs.team_id = m.home_team_id
    LEFT JOIN match_team_stats as_ ON as_.match_id = m.match_id AND as_.team_id = m.away_team_id
    WHERE m.match_id = %s
"""

TACTICAL_SQL = """
    SELECT f.*, t.name AS team_name, o.name AS opponent_name,
           m.match_date, m.season_id, m.home_score, m.away_score
    FROM match_tactical_features f
    JOIN teams t ON t.team_id = f.team_id
    JOIN teams o ON o.team_id = f.opponent_team_id
    JOIN matches m ON m.match_id = f.match_id
    WHERE f.match_id = %s
"""

SEQUENCE_SQL = """
    SELECT s.*, t.name AS team_name, o.name AS opponent_name,
           m.match_date, m.season_id,
           s.start_minute AS minute
    FROM possession_sequences s
    JOIN teams t ON t.team_id = s.team_id
    JOIN matches m ON m.match_id = s.match_id
    JOIN teams o ON o.team_id = CASE WHEN s.team_id = m.home_team_id
                                     THEN m.away_team_id ELSE m.home_team_id END
    WHERE s.match_id = %s
      AND (s.ended_in_shot OR s.events >= %s)
    ORDER BY s.sequence_index
"""

SHOT_SQL = """
    SELECT sh.*, p.name AS player_name, t.name AS team_name,
           o.name AS opponent_name, m.match_date, m.season_id
    FROM shots sh
    JOIN teams t ON t.team_id = sh.team_id
    LEFT JOIN players p ON p.player_id = sh.player_id
    JOIN matches m ON m.match_id = sh.match_id
    JOIN teams o ON o.team_id = CASE WHEN sh.team_id = m.home_team_id
                                     THEN m.away_team_id ELSE m.home_team_id END
    WHERE sh.match_id = %s
    ORDER BY sh.minute
"""

PLAYER_SQL = """
    SELECT ps.*, p.name AS player_name, t.name AS team_name,
           o.name AS opponent_name, m.match_date, m.season_id,
           c.carries, c.progressive_carries, c.carry_progression_m
    FROM match_player_stats ps
    JOIN players p ON p.player_id = ps.player_id
    JOIN teams t ON t.team_id = ps.team_id
    JOIN matches m ON m.match_id = ps.match_id
    JOIN teams o ON o.team_id = CASE WHEN ps.team_id = m.home_team_id
                                     THEN m.away_team_id ELSE m.home_team_id END
    LEFT JOIN (
        SELECT match_id, player_id, count(*) AS carries,
               sum(CASE WHEN is_progressive THEN 1 ELSE 0 END) AS progressive_carries,
               sum(progression_m) AS carry_progression_m
        FROM carries GROUP BY match_id, player_id
    ) c ON c.match_id = ps.match_id AND c.player_id = ps.player_id
    WHERE ps.match_id = %s AND COALESCE(ps.minutes_played, 0) >= %s
    ORDER BY ps.minutes_played DESC
"""

GOALS_SQL = """
    SELECT minute, team_id FROM events
    WHERE match_id = %s AND is_goal = TRUE ORDER BY minute
"""


def composite_source_id(match_id: int, team_or_player_id: int) -> int:
    """A stable synthetic id for documents whose source row has a compound key.

    `semantic_documents` is unique on (doc_type, source_table, source_id), but
    `match_tactical_features` is keyed on (match_id, team_id) and
    `match_player_stats` on (match_id, player_id). Using match_id alone made
    both teams' tactical documents collide, and the second was silently dropped
    by the ON CONFLICT clause -- half the tactical corpus, with no error.
    """
    return int(f'{match_id}{team_or_player_id:07d}')


def _venue_for(team_id, match) -> str:
    return 'H' if team_id == match['home_team_id'] else 'A'


def _opponent_for(team_id, match) -> str:
    return (match['away_team_name'] if team_id == match['home_team_id']
            else match['home_team_name'])


def documents_for_match(cur, match_id: int, *, min_events: int, min_minutes: int
                        ) -> list[dict]:
    """Every document describing one match. Pure read; nothing is written."""
    cur.execute(MATCH_SQL, (match_id,))
    match = cur.fetchone()
    if not match:
        return []

    cur.execute(GOALS_SQL, (match_id,))
    goals = [(int(r['minute'] or 0), r['team_id']) for r in cur.fetchall()]

    common = {'match_id': match_id, 'season_id': match['season_id'],
              'match_date': match['match_date']}
    docs: list[dict] = []

    docs.append({**common, 'doc_type': 'match', 'source_table': 'matches',
                 'source_id': match_id, 'team_id': None,
                 'opponent': match['away_team_name'], 'venue': None,
                 'phase': 'summary', 'period': None, 'player_ids': None,
                 'score_state': None, 'minute': None,
                 'content': gen.match_document(match)})

    cur.execute(TACTICAL_SQL, (match_id,))
    for row in cur.fetchall():
        base = {**common, 'team_id': row['team_id'],
                'opponent': row['opponent_name'],
                'venue': 'H' if row['is_home'] else 'A',
                'period': None, 'player_ids': None, 'score_state': None,
                'minute': None, 'source_table': 'match_tactical_features',
                'source_id': composite_source_id(row['match_id'], row['team_id'])}
        docs.append({**base, 'doc_type': 'tactical', 'phase': 'in-possession',
                     'content': gen.tactical_document(row)})
        docs.append({**base, 'doc_type': 'pressing', 'phase': 'out-of-possession',
                     'content': gen.pressing_document(row)})

    cur.execute(SEQUENCE_SQL, (match_id, min_events))
    for row in cur.fetchall():
        minute = int(row['minute'] or 0)
        docs.append({
            **common, 'doc_type': 'sequence',
            'source_table': 'possession_sequences', 'source_id': row['sequence_id'],
            'team_id': row['team_id'], 'opponent': row['opponent_name'],
            'venue': _venue_for(row['team_id'], match),
            'phase': 'transition' if row['is_high_turnover'] else 'build-up',
            'period': row['period'], 'player_ids': None,
            'score_state': gen.score_state(goals, row['team_id'], minute),
            'minute': minute, 'content': gen.sequence_document(row)})

    cur.execute(SHOT_SQL, (match_id,))
    for row in cur.fetchall():
        minute = int(row['minute'] or 0)
        docs.append({
            **common, 'doc_type': 'shot', 'source_table': 'shots',
            'source_id': row['shot_id'], 'team_id': row['team_id'],
            'opponent': row['opponent_name'],
            'venue': _venue_for(row['team_id'], match),
            'phase': 'set-piece' if row.get('situation') not in (None, 'RegularPlay')
                     else 'attacking',
            'period': None,
            'player_ids': [row['player_id']] if row.get('player_id') else None,
            'score_state': gen.score_state(goals, row['team_id'], minute),
            'minute': minute, 'content': gen.shot_document(row)})

    cur.execute(PLAYER_SQL, (match_id, min_minutes))
    for row in cur.fetchall():
        docs.append({
            **common, 'doc_type': 'player', 'source_table': 'match_player_stats',
            'source_id': None, 'team_id': row['team_id'],
            'opponent': row['opponent_name'],
            'venue': _venue_for(row['team_id'], match),
            'phase': 'player', 'period': None,
            'player_ids': [row['player_id']], 'score_state': None,
            'minute': None, 'content': gen.player_document(row)})

    for d in docs:
        if d['doc_type'] == 'player' and d['source_id'] is None:
            d['source_id'] = composite_source_id(match_id, d['player_ids'][0])

    # Nothing should ever be lost to the conflict clause. If two documents
    # share a key the corpus is silently incomplete, which is exactly how the
    # tactical documents lost one team per match.
    keys = [(d['doc_type'], d['source_table'], d['source_id']) for d in docs]
    if len(set(keys)) != len(keys):
        duplicates = {k for k in keys if keys.count(k) > 1}
        raise ValueError(f'duplicate document keys for match {match_id}: '
                         f'{sorted(duplicates)[:3]}')
    return docs


COLUMNS = ['doc_type', 'source_table', 'source_id', 'content', 'embedding',
           'embedding_model', 'match_id', 'season_id', 'team_id', 'opponent',
           'match_date', 'venue', 'phase', 'period', 'player_ids',
           'score_state', 'minute', 'token_estimate']


def store(cur, match_id: int, docs: list[dict], vectors, model: str) -> int:
    cur.execute('DELETE FROM semantic_documents WHERE match_id = %s', (match_id,))
    rows = []
    for doc, vector in zip(docs, vectors):
        doc = dict(doc)
        doc['embedding'] = vector
        doc['embedding_model'] = model if vector is not None else None
        # A rough token count, enough to budget a prompt without a tokeniser.
        doc['token_estimate'] = max(1, len(doc['content']) // 4)
        rows.append([doc.get(c) for c in COLUMNS])

    execute_values(cur, f"""
        INSERT INTO semantic_documents ({', '.join(COLUMNS)}) VALUES %s
        ON CONFLICT (doc_type, source_table, source_id) DO NOTHING
    """, rows)
    return len(rows)


def build(season: str | None = None, match_id: int | None = None,
          schema: str | None = None, embed_docs: bool = True,
          rebuild: bool = False, min_events: int = gen.SEQUENCE_MIN_EVENTS,
          min_minutes: int = gen.PLAYER_MIN_MINUTES) -> dict:
    schema = schema or current_schema()
    started = time.time()
    totals: dict[str, int] = {}
    written = 0

    with connect(schema=schema) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if rebuild:
            cur.execute('DELETE FROM semantic_documents')
            conn.commit()
            print('cleared the existing corpus')

        if match_id:
            cur.execute('SELECT match_id FROM matches WHERE match_id = %s', (match_id,))
        else:
            cur.execute("""
                SELECT m.match_id FROM matches m
                JOIN seasons s ON s.season_id = m.season_id
                WHERE (%s IS NULL OR s.name = %s) ORDER BY m.match_date
            """, (season, season))
        targets = [r['match_id'] for r in cur.fetchall()]
        print(f'{len(targets)} match(es) in schema {schema}')

        model = 'none'
        if embed_docs:
            from agent import embeddings
            model = embeddings.model_name()

        for n, mid in enumerate(targets, 1):
            docs = documents_for_match(cur, mid, min_events=min_events,
                                       min_minutes=min_minutes)
            if not docs:
                continue

            if embed_docs:
                from agent import embeddings
                vectors = embeddings.embed([d['content'] for d in docs])
                model = embeddings.model_name()
            else:
                vectors = [None] * len(docs)

            written += store(cur, mid, docs, vectors, model)
            conn.commit()

            for d in docs:
                totals[d['doc_type']] = totals.get(d['doc_type'], 0) + 1
            print(f'  [{n}/{len(targets)}] {mid}  {len(docs):4d} documents',
                  flush=True)

    print(f'\n{written:,} documents in {time.time() - started:.1f}s')
    for kind, count in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f'  {kind:10s} {count:6,}')
    return {'written': written, 'by_type': totals}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--season', default=None)
    ap.add_argument('--match', type=int, default=None)
    ap.add_argument('--club', default=None)
    ap.add_argument('--no-embed', action='store_true',
                    help='generate text only, leave embeddings NULL')
    ap.add_argument('--rebuild', action='store_true',
                    help='clear the whole corpus first')
    ap.add_argument('--min-events', type=int, default=gen.SEQUENCE_MIN_EVENTS)
    ap.add_argument('--min-minutes', type=int, default=gen.PLAYER_MIN_MINUTES)
    a = ap.parse_args(argv)

    schema = club_schema(a.club) if a.club else current_schema()
    build(season=a.season, match_id=a.match, schema=schema,
          embed_docs=not a.no_embed, rebuild=a.rebuild,
          min_events=a.min_events, min_minutes=a.min_minutes)
    return 0


if __name__ == '__main__':
    sys.exit(main())
