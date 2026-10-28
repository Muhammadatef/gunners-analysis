"""Retrieval over the semantic documents.

The spec asks for a `Retriever` protocol with a pgvector implementation, so the
backend is a one-file swap if the corpus ever outgrows it. That indirection is
cheap and the reasoning behind it is sound: retrieval is the least load-bearing
part of this system. Numbers come from SQL; retrieval only supplies context.

`PgVectorRetriever` does the filtering and the similarity search in one query,
which is the whole argument for keeping documents in Postgres. "How did Arsenal
set up against City" is a metadata filter on opponent plus a vector search
within it — two systems would need a fan-out and a join in application code,
and would go stale independently.

**Retrieval is hybrid.** `bge-small` is good at paraphrase and bad at tokens
that carry no semantics of their own: "PPDA", "Zone 14", a surname, a query id.
Those are exactly the terms a football question hangs on, so a Postgres
full-text arm runs beside the vector arm and the two rankings are fused with
reciprocal rank fusion. RRF is used rather than a weighted score blend because
cosine similarity and `ts_rank_cd` are not on a common scale and never will be;
ranks are comparable, scores are not. The lexical arm is best-effort — if
`websearch_to_tsquery` or the index is missing, the vector ranking stands on
its own and the caller never sees a failure.

The last step is diversity. Five near-identical sequence documents from the
same passage of play fill the context window with one fact, so the final
selection penalises candidates that repeat what has already been chosen.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg2.extras import RealDictCursor  # noqa: E402

from db.connection import connect, current_schema  # noqa: E402

logger = logging.getLogger(__name__)

# How many candidates each arm contributes before fusion. Twenty is enough for
# the right document to be in reach of a rerank without making the ANN scan
# meaningfully more expensive.
CANDIDATES = int(os.getenv('GUNNERS_RETRIEVE_CANDIDATES', '20'))

# RRF's smoothing constant. 60 is the value from the original paper and the
# usual default; it flattens the difference between rank 1 and rank 2 enough
# that one arm cannot dominate on a single confident hit.
RRF_K = int(os.getenv('GUNNERS_RRF_K', '60'))

# 1.0 keeps pure relevance; lower values trade some of it for diversity.
MMR_LAMBDA = float(os.getenv('GUNNERS_MMR_LAMBDA', '0.7'))

HYBRID = os.getenv('GUNNERS_HYBRID_RETRIEVAL', '1') not in ('0', 'false', 'no')


@dataclass
class Document:
    content: str
    doc_type: str
    score: float
    match_id: int | None = None
    opponent: str | None = None
    match_date: Any = None
    minute: int | None = None
    phase: str | None = None
    metadata: dict = field(default_factory=dict)

    def as_prompt(self) -> str:
        head = f'[{self.doc_type}'
        if self.opponent:
            head += f' vs {self.opponent}'
        if self.match_date:
            head += f', {self.match_date}'
        return f'{head}] {self.content}'

    def as_dict(self) -> dict:
        return {'content': self.content, 'doc_type': self.doc_type,
                'score': self.score, 'match_id': self.match_id,
                'opponent': self.opponent, 'phase': self.phase}


@runtime_checkable
class Retriever(Protocol):
    def search(self, query: str, k: int = 5,
               filters: dict | None = None) -> list[Document]: ...


# Only these may be filtered on. An allowlist for the same reason the SQL
# registry is one: the filter dict is assembled from model output.
FILTERABLE = {'doc_type', 'match_id', 'opponent', 'team_id', 'venue', 'phase',
              'period', 'score_state', 'season_id'}

_COLUMNS = """content, doc_type, match_id, opponent, match_date, minute,
              phase, team_id, score_state"""


@functools.lru_cache(maxsize=8)
def _fts_column(schema: str) -> str:
    """`content_tsv` when the migration has been applied, else an expression.

    Computing `to_tsvector` per row at query time works and is what an
    un-migrated database gets; it just cannot use an index. Detecting the
    column keeps the code path identical either way.
    """
    try:
        with connect(schema=schema) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'semantic_documents'
                  AND column_name = 'content_tsv'
                  AND table_schema = %s
            """, (schema,))
            if cur.fetchone():
                return 'content_tsv'
    except Exception as exc:                                    # noqa: BLE001
        logger.debug('fts column probe failed: %s', exc)
    return "to_tsvector('english', content)"


class PgVectorRetriever:
    """Hybrid search over `semantic_documents`, with metadata pre-filtering."""

    def __init__(self, schema: str | None = None, embed_fn=None,
                 hybrid: bool | None = None):
        self.schema = schema or current_schema()
        self._embed_fn = embed_fn
        self.hybrid = HYBRID if hybrid is None else hybrid

    def _embed(self, query: str) -> list[float]:
        if self._embed_fn is not None:
            return self._embed_fn(query)
        from agent import embeddings
        return embeddings.embed_query(query)

    # -- SQL assembly ------------------------------------------------------

    def _where(self, filters: dict | None) -> tuple[list[str], dict]:
        clauses = ['embedding IS NOT NULL']
        params: dict[str, Any] = {}
        for key, value in (filters or {}).items():
            if key not in FILTERABLE or value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                clauses.append(f'{key} = ANY(%({key})s)')
                params[key] = list(value)
            elif key == 'opponent':
                # Club naming varies between feeds; match loosely on this one.
                clauses.append('opponent ILIKE %(opponent)s')
                params['opponent'] = f'%{value}%'
            else:
                clauses.append(f'{key} = %({key})s')
                params[key] = value
        return clauses, params

    def _vector_arm(self, cur, query: str, n: int,
                    clauses: list[str], params: dict) -> list[dict]:
        sql = f"""
            SELECT {_COLUMNS},
                   1 - (embedding <=> %(vec)s::vector) AS score
            FROM semantic_documents
            WHERE {' AND '.join(clauses)}
            ORDER BY embedding <=> %(vec)s::vector
            LIMIT %(n)s
        """
        cur.execute(sql, {**params, 'vec': self._embed(query), 'n': n})
        return [dict(r) for r in cur.fetchall()]

    def _lexical_arm(self, cur, query: str, n: int,
                     clauses: list[str], params: dict) -> list[dict]:
        """Best effort: a failure here degrades to vector-only, never raises."""
        terms = _lexical_terms(query)
        if not terms:
            return []
        tsv = _fts_column(self.schema)
        sql = f"""
            SELECT {_COLUMNS},
                   ts_rank_cd({tsv}, q) AS score
            FROM semantic_documents,
                 websearch_to_tsquery('english', %(q)s) AS q
            WHERE {' AND '.join(clauses)} AND {tsv} @@ q
            ORDER BY score DESC
            LIMIT %(n)s
        """
        try:
            cur.execute(sql, {**params, 'q': terms, 'n': n})
            return [dict(r) for r in cur.fetchall()]
        except Exception as exc:                                # noqa: BLE001
            logger.debug('lexical arm unavailable (%s); vector only', exc)
            cur.connection.rollback()
            return []

    # -- the search itself -------------------------------------------------

    def search(self, query: str, k: int = 5, filters: dict | None = None,
               candidates: int | None = None) -> list[Document]:
        clauses, params = self._where(filters)
        n = max(k, candidates or CANDIDATES)

        with connect(schema=self.schema) as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('SET TRANSACTION READ ONLY')
            dense = self._vector_arm(cur, query, n, clauses, params)
            sparse = (self._lexical_arm(cur, query, n, clauses, params)
                      if self.hybrid else [])

        if not sparse:
            ranked = dense
        else:
            ranked = fuse([dense, sparse])

        chosen = diversify(ranked, k)
        return [_document(row) for row in chosen]

    def count(self) -> int:
        with connect(schema=self.schema) as conn:
            cur = conn.cursor()
            cur.execute('SELECT count(*) FROM semantic_documents WHERE embedding IS NOT NULL')
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# fusion, diversity, and the small helpers they need
# ---------------------------------------------------------------------------

def _key(row: dict) -> tuple:
    """Identity of a document across the two arms.

    `semantic_documents` has no id exposed by these queries, and (match, type,
    minute, content) is unique enough — two rows that agree on all four are the
    same document by any definition that matters to a prompt.
    """
    return (row.get('match_id'), row.get('doc_type'), row.get('minute'),
            row.get('content'))


def fuse(rankings: list[list[dict]], k: int = RRF_K) -> list[dict]:
    """Reciprocal rank fusion over one ranking per arm.

    Score is `sum(1 / (k + rank))`, which needs no calibration between arms:
    a document ranked third by both beats one ranked first by only one, and
    neither arm's score distribution enters into it.
    """
    scores: dict[tuple, float] = {}
    rows: dict[tuple, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            identity = _key(row)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (k + rank)
            rows.setdefault(identity, row)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    out = []
    for identity, score in ordered:
        row = dict(rows[identity])
        row['score'] = round(score, 6)
        out.append(row)
    return out


_TOKEN = re.compile(r'[a-z0-9]{3,}')


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall((text or '').lower()))


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def diversify(rows: list[dict], k: int, lambda_: float = MMR_LAMBDA) -> list[dict]:
    """Maximal-marginal-relevance selection over the fused ranking.

    Similarity is token overlap rather than cosine over the stored vectors:
    the documents are short and factual, the near-duplicates this exists to
    catch are near-duplicates lexically, and it avoids pulling 384 floats per
    candidate back out of Postgres to re-derive something the text already says.
    """
    if k <= 0 or not rows:
        return []
    if lambda_ >= 1.0:
        return rows[:k]

    pool = [(row, _tokens(row.get('content', ''))) for row in rows]
    # Rank position stands in for relevance, so the two terms share a scale.
    relevance = {id(row): 1.0 / (1 + i) for i, (row, _) in enumerate(pool)}

    chosen: list[tuple[dict, frozenset[str]]] = [pool[0]]
    remaining = pool[1:]
    while remaining and len(chosen) < k:
        best, best_score = None, None
        for candidate in remaining:
            row, tokens = candidate
            penalty = max((_overlap(tokens, picked) for _, picked in chosen),
                          default=0.0)
            score = lambda_ * relevance[id(row)] - (1 - lambda_) * penalty
            if best_score is None or score > best_score:
                best, best_score = candidate, score
        chosen.append(best)
        remaining.remove(best)
    return [row for row, _ in chosen]


_STOPWORDS = frozenset({
    'what', 'when', 'where', 'which', 'who', 'whom', 'how', 'why', 'did',
    'does', 'was', 'were', 'the', 'and', 'for', 'from', 'with', 'that',
    'this', 'they', 'their', 'them', 'about', 'into', 'over', 'many', 'much',
    'tell', 'show', 'give', 'against', 'during',
})


def _lexical_terms(query: str) -> str:
    """The part of a question worth matching literally.

    `websearch_to_tsquery` ANDs its terms, so feeding it a whole sentence
    returns nothing at all. Dropping question words and punctuation leaves the
    nouns the lexical arm exists to catch.
    """
    words = [w for w in _TOKEN.findall((query or '').lower())
             if w not in _STOPWORDS]
    return ' or '.join(words[:12])


def _document(row: dict) -> Document:
    return Document(
        content=row['content'], doc_type=row['doc_type'],
        score=float(row.get('score') or 0.0), match_id=row.get('match_id'),
        opponent=row.get('opponent'), match_date=row.get('match_date'),
        minute=row.get('minute'), phase=row.get('phase'),
        metadata={'team_id': row.get('team_id'),
                  'score_state': row.get('score_state')},
    )
