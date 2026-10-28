"""Deterministic routing: decide without the model whenever the text is plain.

`IntentClassifier` used to spend an LM call — and the *entire* SQL catalogue in
the prompt — on questions whose answer is obvious from the words. "How many
goals did Saka score" is not a reasoning problem; it is a lookup with two
slots. `docs/AGENT_EFFICIENCY.md` measured the cost of pretending otherwise:
routing is the cheap tier's bottleneck, and every classify pays for a catalogue
of fifteen queries plus a reasoning essay.

Three things live here, in increasing order of how much they cost:

    fast_route      pure text -> Route, or None. No model, no network.
    shortlist       the 4-6 query ids worth showing the model, by embedding.
    merge_slots     carry opponent / season / match_id across a conversation.

`fast_route` is deliberately conservative. Returning `None` is cheap — the LM
picks up the question — while a confident wrong route is a wrong answer, so
every rule here requires both a recognised intent word *and* the slots that
query needs. Player names are matched against the warehouse rather than
guessed, which is what makes the player families safe to fast-path at all.
"""

from __future__ import annotations

import functools
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.tools import sql_registry  # noqa: E402

DEFAULT_TEAM = os.getenv('GUNNERS_TEAM', 'Arsenal')
DEFAULT_SEASON = sql_registry.normalise_season(
    os.getenv('GUNNERS_SEASON', '2025/2026'))

TOOLS = ('sql', 'retrieval', 'report', 'none')

# How many catalogue entries the model is shown. The full list is fifteen
# queries and grows; six is enough for the right one to be present in practice
# and cuts the classify prompt by roughly two thirds.
SHORTLIST_K = int(os.getenv('GUNNERS_ROUTER_SHORTLIST', '6'))


@dataclass
class Route:
    """What to do with a question, and where that decision came from."""

    tool: str
    query_id: str = ''
    parameters: dict = field(default_factory=dict)
    reasoning: str = ''
    source: str = 'fast'          # fast | lm | lm-fallback | caller

    def as_dict(self) -> dict:
        return {'tool': self.tool, 'query_id': self.query_id,
                'parameters': dict(self.parameters),
                'reasoning': self.reasoning, 'source': self.source}


# ---------------------------------------------------------------------------
# lexicon
# ---------------------------------------------------------------------------

_SEASON_RE = re.compile(r'\b(20\d{2})\s*[/-]\s*(\d{2}|\d{4})\b')
_GW_RE = re.compile(r'\b(?:gw|g\.?w\.?|mw|matchweek|gameweek)\s*(\d{1,2})\b', re.I)

# Questions the warehouse cannot answer at all. Refusing is a correct answer,
# and refusing without an LM call is a free one.
_REFUSE = re.compile(
    r'\b(injur\w*|fitness|doubtful|suspend\w*|transfer|transfer fee|sign(?:ing|ed)\b'
    r'|contract|wages?|salary|market value|release clause|loan deal'
    r'|next season|next year|will \w+ win|predict\w*|forecast|odds'
    r'|xg (?:of|for) (?:that|the|this) shot|per-?shot xg|shot xg)\b', re.I)

_REPORT = re.compile(
    r'\b(report|briefing|write[- ]?up|technical analysis|'
    r'analyse the match|analyze the match|post[- ]?match)\b', re.I)

# Prose questions — "how", "why", "describe" without a metric noun.
_NARRATIVE = re.compile(
    r'\b(how did|how do|why did|why was|describe|explain|talk me through|'
    r'walk me through|what happened|set up|line ?up|approach|game ?plan|'
    r'tactics?|shape|narrative|story)\b', re.I)

_METRIC_WORDS = re.compile(
    r'\b(goals?|assists?|minutes?|appearances?|starts?|points?|xg|xgot|'
    r'possession|shots?|passes|passing|duels?|ppda|press(?:es|ing)?|'
    r'carries|carried|touches|per ?90|clean sheets?|record|scored|conceded)\b',
    re.I)

_SCORE_WORDS = re.compile(
    r'\b(score|scoreline|result|final score|win|won|lose|lost|draw|drew|'
    r'beat|beaten|outcome)\b', re.I)

_PLAYER_METRIC = re.compile(
    r'\b(goals?|assists?|minutes?|appearances?|starts?|scored|per ?90|'
    r'shots?|passes|touches|carries|rating)\b', re.I)


def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '')).strip()


def extract_season(question: str, default: str | None = None) -> str:
    m = _SEASON_RE.search(question or '')
    if m:
        return sql_registry.normalise_season(f'{m.group(1)}/{m.group(2)}')
    return sql_registry.normalise_season(default or DEFAULT_SEASON)


def extract_matchweek(question: str) -> int | None:
    m = _GW_RE.search(question or '')
    return int(m.group(1)) if m else None


def extract_opponent(question: str) -> str:
    """Opponent from the question text, via the report module's club lexicon.

    Imported lazily: `agent.report` pulls in the visualiser, and this module is
    imported by DAG-parsed code where that cost would be paid repeatedly.
    """
    try:
        from agent.report import try_parse_report_slots_fast
    except Exception:                                           # noqa: BLE001
        return ''
    slots = try_parse_report_slots_fast(question or '', DEFAULT_SEASON)
    return (slots or {}).get('opponent') or ''


# ---------------------------------------------------------------------------
# player names, from the warehouse rather than from guesswork
# ---------------------------------------------------------------------------

_PLAYERS_TTL_S = float(os.getenv('GUNNERS_PLAYER_CACHE_TTL', '900'))
_players_cache: dict[str | None, tuple[float, list[str]]] = {}
_players_lock = threading.Lock()

# Surnames that are also ordinary English words. Matching one of these on its
# own is how "how many clean sheets" becomes a query about a player, so they
# only count when the full name is written out.
_AMBIGUOUS_SURNAMES = frozenset({
    'white', 'rice', 'timber', 'martin', 'young', 'king', 'walker', 'wood',
    'brown', 'green', 'cash', 'long', 'ward', 'moore', 'reed', 'bright',
})


def known_players(schema: str | None = None) -> list[str]:
    """Distinct player names, cached for a few minutes.

    A DB round trip per route would be silly; a stale name list for fifteen
    minutes costs nothing, because squads do not change mid-conversation.
    """
    now = time.time()
    with _players_lock:
        hit = _players_cache.get(schema)
        if hit and now - hit[0] < _PLAYERS_TTL_S:
            return hit[1]
    names: list[str] = []
    try:
        from db.connection import connect
        with connect(schema=schema) as conn:
            cur = conn.cursor()
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute('SELECT DISTINCT player_name FROM v_player_season_totals '
                        'WHERE player_name IS NOT NULL')
            names = [r[0] for r in cur.fetchall() if r[0]]
    except Exception:                                           # noqa: BLE001
        names = []
    with _players_lock:
        _players_cache[schema] = (now, names)
    return names


def extract_player(question: str, schema: str | None = None) -> str:
    """The warehouse player named in the question, or ''.

    Longest match wins, so "Gabriel Martinelli" is not read as "Gabriel".
    """
    q = f' {_clean(question).lower()} '
    if not q.strip():
        return ''
    best = ''
    for name in known_players(schema):
        low = name.lower()
        if re.search(rf'(?<![\w]){re.escape(low)}(?![\w])', q):
            if len(low) > len(best):
                best = name
            continue
        parts = [p for p in re.split(r'[\s.\-]+', low) if len(p) >= 4]
        surname = parts[-1] if parts else ''
        if (surname and surname not in _AMBIGUOUS_SURNAMES
                and re.search(rf'(?<![\w]){re.escape(surname)}(?![\w])', q)
                and len(surname) > len(best)):
            best = name
    return best


# ---------------------------------------------------------------------------
# the fast path
# ---------------------------------------------------------------------------

def fast_route(question: str, slots: dict | None = None,
               schema: str | None = None) -> Route | None:
    """A route decided from the text alone, or None to let the model decide."""
    q = _clean(question)
    if not q:
        return None
    slots = slots or {}
    season = extract_season(q, slots.get('season'))
    team = slots.get('team') or DEFAULT_TEAM
    lowered = q.lower()

    if _REFUSE.search(q):
        return Route('none', reasoning='Outside what the warehouse holds '
                                       '(squad status, market, or the future)')

    if _REPORT.search(q):
        opponent = extract_opponent(q) or slots.get('opponent') or ''
        if opponent or slots.get('match_id'):
            return Route('report', 'match_report',
                         {'opponent': opponent, 'season': season,
                          'matchweek': extract_matchweek(q),
                          'match_id': slots.get('match_id')},
                         'Match-report request with a resolved fixture')
        return None

    opponent = extract_opponent(q) or ('' if _season_scoped(lowered)
                                       else slots.get('opponent') or '')
    player = extract_player(q, schema) if _PLAYER_METRIC.search(q) else ''

    # -- one player -------------------------------------------------------
    if player:
        if opponent:
            return Route('sql', 'player_match_stats',
                         {'player': player, 'opponent': opponent, 'season': season},
                         f'{player} in the {opponent} fixture')
        return Route('sql', 'player_season_totals',
                     {'player': player, 'season': season},
                     f'{player} season totals')

    # -- squad leaderboards ------------------------------------------------
    leaderboard = _leaderboard_route(lowered, team, season)
    if leaderboard is not None:
        return leaderboard

    # -- one match ---------------------------------------------------------
    if opponent:
        match_route = _match_route(lowered, opponent, season)
        if match_route is not None:
            return match_route

    # -- one season --------------------------------------------------------
    season_route = _season_route(lowered, team, season)
    if season_route is not None:
        return season_route

    # -- prose -------------------------------------------------------------
    if _NARRATIVE.search(q) and not _METRIC_WORDS.search(q):
        params: dict[str, Any] = {}
        if opponent:
            params['opponent'] = opponent
        return Route('retrieval', '', params,
                     'Descriptive question — answered from documents')

    return None


def _season_scoped(lowered: str) -> bool:
    return bool(re.search(r'\b(season|campaign|all season|this year|overall|'
                          r'in total|across the season)\b', lowered))


def _leaderboard_route(lowered: str, team: str, season: str) -> Route | None:
    # "lead" on its own is a scoreline word ("did we take the lead?"), so only
    # the leaderboard phrasings of it count.
    top = re.search(r'\b(top|best|leading|most|highest|furthest|longest)\b'
                    r'|\bwho (?:leads|led|has the most)\b', lowered)
    if not top:
        return None
    base = {'team': team, 'season': season, 'limit': 5}

    if re.search(r'\b(scorer|scorers|goals?)\b', lowered):
        return Route('sql', 'top_scorers', base, 'Leading scorers this season')
    if re.search(r'\bassists?\b', lowered):
        return Route('sql', 'top_by_metric',
                     {**base, 'metric': 'assists_per_90', 'min_minutes': 450},
                     'Squad leaders by assists per 90')
    if re.search(r'\b(progressive|prog) pass', lowered):
        return Route('sql', 'top_by_metric',
                     {**base, 'metric': 'prog_passes_per_90', 'min_minutes': 450},
                     'Squad leaders by progressive passes per 90')
    if re.search(r'\b(xt|expected threat|threat)\b', lowered):
        return Route('sql', 'top_by_metric',
                     {**base, 'metric': 'xt_per_90', 'min_minutes': 450},
                     'Squad leaders by expected threat per 90')
    if re.search(r'\b(carr(?:y|ies|ied|ier|iers)|ball carr)', lowered):
        return Route('sql', 'top_carriers', base, 'Most ball progression by carry')
    if re.search(r'\b(sequence|possession|touches)\b', lowered):
        return Route('sql', 'longest_sequences', base,
                     'Longest possessions of the season')
    if re.search(r'\b(win|victor|thrash|rout)\w*\b', lowered):
        return Route('sql', 'biggest_wins', base, 'Largest victories this season')
    return None


def _match_route(lowered: str, opponent: str, season: str) -> Route | None:
    base = {'opponent': opponent, 'season': season}

    if re.search(r'\b(every shot|all the shots|shot map|list the shots|'
                 r'shots taken|shot by shot)\b', lowered):
        return Route('sql', 'shots_in_match', base, f'Shots against {opponent}')
    if re.search(r'\b(xg|xgot|expected goals)\b', lowered):
        return Route('sql', 'match_xg', base, f'xG in the {opponent} fixture')
    if re.search(r'\b(ppda|press(?:ing|es)?|high line|build[- ]?up|'
                 r'field tilt|directness)\b', lowered):
        return Route('sql', 'tactical_features', base,
                     f'Pressing and shape against {opponent}')
    if re.search(r'\b(possession|shots?|passing|passes|duels?|accuracy|'
                 r'corners?|stats|statistics)\b', lowered):
        return Route('sql', 'team_match_stats', base,
                     f'Match statistics against {opponent}')
    if _SCORE_WORDS.search(lowered) or re.search(r'\b(when did|what date)\b', lowered):
        return Route('sql', 'match_result', base, f'Result against {opponent}')
    return None


def _season_route(lowered: str, team: str, season: str) -> Route | None:
    base = {'team': team, 'season': season}

    if re.search(r'\b(ppda|press(?:ing|es)?)\b', lowered):
        return Route('sql', 'season_pressing', base, 'Season pressing averages')
    if re.search(r'\b(home and away|home record|away record|at home|'
                 r'on the road|venue)\b', lowered):
        return Route('sql', 'results_by_venue', base, 'Home and away record')
    if re.search(r'\b(set[- ]?piece|corners?|free[- ]?kicks?|dead ball)\b', lowered):
        return Route('sql', 'set_piece_shots', base, 'Set-piece shots this season')
    if re.search(r'\b(points?|league position|table|won drawn lost|'
                 r'goal difference|season record|how did .* do|'
                 r'goals scored|goals conceded|clean sheets?)\b', lowered):
        return Route('sql', 'season_summary', base, 'Season record')
    if _season_scoped(lowered) and _METRIC_WORDS.search(lowered):
        return Route('sql', 'season_summary', base, 'Season record')
    return None


# ---------------------------------------------------------------------------
# catalogue shortlist — what the model is allowed to see
# ---------------------------------------------------------------------------

def _catalogue_texts() -> list[tuple[str, str]]:
    return [(q.id, f'{q.id.replace("_", " ")}: {q.description}')
            for q in sql_registry.REGISTRY.values()]


@functools.lru_cache(maxsize=1)
def _catalogue_vectors() -> tuple[tuple[str, ...], Any]:
    """Embed the catalogue once per process. Returns (ids, matrix) or (ids, None)."""
    ids = tuple(qid for qid, _ in _catalogue_texts())
    try:
        from agent import embeddings
        vectors = embeddings.embed([text for _, text in _catalogue_texts()])
        return ids, vectors
    except Exception:                                           # noqa: BLE001
        return ids, None


@functools.lru_cache(maxsize=512)
def _query_vector(question: str):
    try:
        from agent import embeddings
        return embeddings.embed_query(question)
    except Exception:                                           # noqa: BLE001
        return None


def _cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


def _keyword_scores(question: str) -> list[tuple[str, float]]:
    """Fallback ranking when fastembed is unavailable — token overlap."""
    wanted = set(re.findall(r'[a-z0-9]{3,}', (question or '').lower()))
    scored = []
    for qid, text in _catalogue_texts():
        tokens = set(re.findall(r'[a-z0-9]{3,}', text.lower()))
        scored.append((qid, float(len(wanted & tokens))))
    return scored


def shortlist(question: str, limit: int = SHORTLIST_K,
              always: tuple[str, ...] = ()) -> list[str]:
    """The query ids worth putting in front of the model, best first."""
    ids, matrix = _catalogue_vectors()
    vector = _query_vector(_clean(question)) if matrix is not None else None
    if matrix is not None and vector is not None:
        scored = [(qid, _cosine(vector, row)) for qid, row in zip(ids, matrix)]
    else:
        scored = _keyword_scores(question)
    scored.sort(key=lambda kv: -kv[1])

    chosen: list[str] = [q for q in always if q in sql_registry.REGISTRY]
    for qid, _ in scored:
        if len(chosen) >= limit:
            break
        if qid not in chosen:
            chosen.append(qid)
    return chosen


def catalogue_for(question: str, limit: int = SHORTLIST_K,
                  always: tuple[str, ...] = ()) -> str:
    """The shortlisted catalogue, rendered the way the classifier expects."""
    return sql_registry.describe(shortlist(question, limit, always))


def warm() -> bool:
    """Pre-load the embedding model and catalogue vectors.

    Called from the analyst service's lifespan so the first user question does
    not pay a fastembed model load inside the request.
    """
    _, matrix = _catalogue_vectors()
    return matrix is not None


# ---------------------------------------------------------------------------
# conversation slots
# ---------------------------------------------------------------------------

CARRIED = ('match_id', 'opponent', 'season', 'player', 'team', 'matchweek')

# "What about him in the second half" is a follow-up; "How many goals did
# Arsenal score in 2025/2026" is not, even as the fourth message.
_FOLLOW_UP = re.compile(
    r'\b(what about|and (?:him|her|them|that|the)|same (?:for|question)|'
    r'in that (?:match|game)|that (?:match|game|fixture)|there|then|'
    r'him|her|them|they|it)\b', re.I)


def looks_like_follow_up(question: str) -> bool:
    q = _clean(question)
    if not q:
        return False
    return bool(_FOLLOW_UP.search(q)) or len(q.split()) <= 5


def merge_slots(parameters: dict | None, slots: dict | None,
                question: str = '') -> dict:
    """Fill parameters the current question left out from the previous turn.

    Slots, not a transcript. Passing the last few messages back into the prompt
    would cost tokens on every turn and invites the model to reuse a number it
    saw rather than one a tool vouched for; carrying `opponent` / `season` /
    `match_id` gives follow-ups what they actually need.
    """
    merged = dict(parameters or {})
    if not slots:
        return merged
    carry_identity = looks_like_follow_up(question)
    for key in CARRIED:
        value = slots.get(key)
        if value in (None, '', []):
            continue
        if key in ('player', 'opponent') and not carry_identity:
            continue
        merged.setdefault(key, value)
    return merged


def slots_from_message(message: dict | None) -> dict:
    """Pull carryable slots out of a stored assistant message."""
    if not message:
        return {}
    params = message.get('parameters') or {}
    if isinstance(params, str):
        import json
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    out = {k: params.get(k) for k in CARRIED if params.get(k) not in (None, '', [])}
    return out
