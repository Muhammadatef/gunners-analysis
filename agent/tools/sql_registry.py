"""The allowlist: every SQL query the agent is permitted to run.

This file is the anti-hallucination guarantee, and it is worth being precise
about what that means. The model does not write SQL. It chooses an id from this
registry and supplies parameters, which are bound by psycopg2 rather than
interpolated. The worst mistake available to it is choosing the *wrong* query —
a routing error, which the evaluation set measures and which shows up as a
visibly odd answer — rather than inventing a number, which would not show up at
all.

Three consequences follow, and all three are the point:

* **No text-to-SQL.** A generated query can be subtly wrong in ways nobody
  notices: a missing season filter, a join that doubles rows, an average over
  the wrong grain. Every query here was written once and can be reviewed once.
* **Every result carries provenance.** `run()` returns an `Evidence` object
  holding the query id, the parameters and the value. An answer's numbers are
  checked against those before it is returned.
* **The evaluation set shares this registry.** Ground truth for a statistical
  question is produced by running the same query the agent would run, so the
  eval measures routing and faithfulness rather than re-testing SQL that the
  warehouse's own tests already cover.

Queries are read-only by construction — every one begins with SELECT, and
`run()` refuses anything else and executes inside a read-only transaction.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg2.extras import RealDictCursor  # noqa: E402

from db.connection import connect, current_schema  # noqa: E402


@dataclass(frozen=True)
class Query:
    """One permitted question of the warehouse."""

    id: str
    description: str          # what the router sees when choosing
    sql: str
    params: tuple[str, ...] = ()
    returns: str = 'rows'     # scalar | row | rows
    unit: str | None = None   # for rendering and for eval comparison


@dataclass
class Evidence:
    """A value, and exactly where it came from.

    Carried through the whole answer path so that a number in the final text
    can be traced to the query that produced it. This is what makes the
    hallucination check mechanical instead of a matter of opinion.
    """

    query_id: str
    params: dict[str, Any]
    value: Any
    description: str
    unit: str | None = None
    rows: list[dict] = field(default_factory=list)

    def numbers(self) -> set[float]:
        """Every numeric value this evidence vouches for."""
        from decimal import Decimal

        found: set[float] = set()

        def walk(item):
            if isinstance(item, bool) or item is None:
                return
            if isinstance(item, (int, float, Decimal)):
                found.add(round(float(item), 4))
            elif isinstance(item, dict):
                for v in item.values():
                    walk(v)
            elif isinstance(item, (list, tuple)):
                for v in item:
                    walk(v)
            elif isinstance(item, str):
                for token in re.findall(r'-?\d+\.?\d*', item):
                    try:
                        found.add(round(float(token), 4))
                    except ValueError:
                        pass

        walk(self.value)
        walk(self.rows)
        return found

    def as_prompt(self) -> str:
        """How this evidence is shown to the model."""
        head = f'[{self.query_id}] {self.description}'
        if self.returns_scalar:
            return f'{head}: {self.value}{" " + self.unit if self.unit else ""}'
        return f'{head}: {self.value}'

    @property
    def returns_scalar(self) -> bool:
        return not isinstance(self.value, (list, dict))


# ---------------------------------------------------------------------------
# the allowlist
# ---------------------------------------------------------------------------

_QUERIES: list[Query] = [
    Query(
        id='match_result',
        description="A match's final score, date and competition, by opponent and season",
        params=('opponent', 'season'),
        returns='rows',
        sql="""
            SELECT m.match_id, m.match_date, m.matchweek,
                   h.name AS home_team, a.name AS away_team,
                   m.home_score, m.away_score
            FROM matches m
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id
            WHERE s.name = %(season)s
              AND (h.name ILIKE %(opponent)s OR a.name ILIKE %(opponent)s)
            ORDER BY m.match_date
        """),
    Query(
        id='match_xg',
        description='Expected goals and xGOT for both teams in a match',
        params=('opponent', 'season'),
        sql="""
            SELECT m.match_date, h.name AS home_team, a.name AS away_team,
                   m.home_xg, m.away_xg, m.home_xgot, m.away_xgot, m.xg_source
            FROM matches m
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id
            WHERE s.name = %(season)s
              AND (h.name ILIKE %(opponent)s OR a.name ILIKE %(opponent)s)
            ORDER BY m.match_date
        """),
    Query(
        id='season_summary',
        description="A team's season record: played, won, drawn, lost, goals, points, xG",
        params=('team', 'season'),
        returns='row',
        sql="""
            SELECT v.played, v.won, v.drawn, v.lost, v.goals_for,
                   v.goals_against, v.goal_difference, v.points, v.total_xg,
                   v.avg_possession, v.avg_field_tilt
            FROM v_team_season_summary v
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.team_name ILIKE %(team)s AND s.name = %(season)s
        """),
    Query(
        id='player_season_totals',
        description="A player's season totals: appearances, minutes, goals, assists, per-90s",
        params=('player', 'season'),
        returns='row',
        sql="""
            SELECT v.player_name, v.team_name, v.appearances, v.starts, v.minutes,
                   v.goals, v.assists, v.shots, v.key_passes,
                   v.progressive_passes, v.passes, v.passes_accurate,
                   v.tackles, v.interceptions, v.ball_recoveries,
                   v.total_xt, v.avg_rating, v.goals_per_90, v.assists_per_90,
                   v.prog_passes_per_90, v.xt_per_90
            FROM v_player_season_totals v
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.player_name ILIKE %(player)s AND s.name = %(season)s
        """),
    Query(
        id='top_scorers',
        description='Leading goalscorers for a team in a season',
        params=('team', 'season', 'limit'),
        sql="""
            SELECT v.player_name, v.goals, v.assists, v.minutes, v.goals_per_90
            FROM v_player_season_totals v
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.team_name ILIKE %(team)s AND s.name = %(season)s
            ORDER BY v.goals DESC, v.minutes ASC
            LIMIT %(limit)s
        """),
    Query(
        id='top_by_metric',
        description=('Squad leaders in one per-90 metric. metric must be one of '
                     'xt_per_90, prog_passes_per_90, goals_per_90, assists_per_90'),
        params=('team', 'season', 'metric', 'min_minutes', 'limit'),
        sql="""
            SELECT v.player_name, v.minutes,
                   CASE %(metric)s
                       WHEN 'xt_per_90' THEN v.xt_per_90
                       WHEN 'prog_passes_per_90' THEN v.prog_passes_per_90
                       WHEN 'goals_per_90' THEN v.goals_per_90
                       WHEN 'assists_per_90' THEN v.assists_per_90
                   END AS value
            FROM v_player_season_totals v
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.team_name ILIKE %(team)s AND s.name = %(season)s
              AND v.minutes >= %(min_minutes)s
            ORDER BY value DESC NULLS LAST
            LIMIT %(limit)s
        """),
    Query(
        id='team_match_stats',
        description='Possession, shots, passing and duels for both teams in a match',
        params=('opponent', 'season'),
        sql="""
            SELECT t.name AS team, ts.possession_pct, ts.field_tilt_pct,
                   ts.shots, ts.shots_on_target, ts.big_chances,
                   ts.passes, ts.passes_accurate, ts.progressive_passes,
                   ts.corners, ts.tackles, ts.interceptions, ts.xt
            FROM match_team_stats ts
            JOIN matches m ON m.match_id = ts.match_id
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams t ON t.team_id = ts.team_id
            JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id
            WHERE s.name = %(season)s
              AND (h.name ILIKE %(opponent)s OR a.name ILIKE %(opponent)s)
            ORDER BY m.match_date, ts.is_home DESC
        """),
    Query(
        id='tactical_features',
        description=('Pressing and possession shape for a match: PPDA, '
                     'counterpresses, field tilt, sequence length, directness'),
        params=('opponent', 'season'),
        sql="""
            SELECT t.name AS team, o.name AS opponent, m.match_date,
                   f.ppda, f.ppda_allowed, f.counterpresses, f.high_turnovers,
                   f.field_tilt_pct, f.sequences, f.avg_sequence_events,
                   f.direct_speed_ms, f.long_sequences, f.sequences_ending_in_shot
            FROM match_tactical_features f
            JOIN matches m ON m.match_id = f.match_id
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams t ON t.team_id = f.team_id
            JOIN teams o ON o.team_id = f.opponent_team_id
            WHERE s.name = %(season)s AND o.name ILIKE %(opponent)s
            ORDER BY m.match_date
        """),
    Query(
        id='season_pressing',
        description="A team's average pressing metrics across a season",
        params=('team', 'season'),
        returns='row',
        sql="""
            SELECT round(avg(f.ppda), 2) AS avg_ppda,
                   round(avg(f.field_tilt_pct), 2) AS avg_field_tilt,
                   sum(f.counterpresses) AS total_counterpresses,
                   sum(f.high_turnovers) AS total_high_turnovers,
                   round(avg(f.avg_sequence_events), 2) AS avg_sequence_events,
                   count(*) AS matches
            FROM match_tactical_features f
            JOIN matches m ON m.match_id = f.match_id
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams t ON t.team_id = f.team_id
            WHERE t.name ILIKE %(team)s AND s.name = %(season)s
        """),
    Query(
        id='player_match_stats',
        description="One player's numbers in one match",
        params=('player', 'opponent', 'season'),
        sql="""
            SELECT p.name AS player, o.name AS opponent, m.match_date,
                   ps.minutes_played, ps.goals, ps.assists, ps.shots,
                   ps.key_passes, ps.passes, ps.passes_accurate,
                   ps.progressive_passes, ps.take_ons, ps.take_ons_won,
                   ps.tackles, ps.interceptions, ps.ball_recoveries,
                   ps.xt, ps.rating
            FROM match_player_stats ps
            JOIN players p ON p.player_id = ps.player_id
            JOIN matches m ON m.match_id = ps.match_id
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams o ON o.team_id = CASE WHEN ps.team_id = m.home_team_id
                                             THEN m.away_team_id ELSE m.home_team_id END
            WHERE p.name ILIKE %(player)s AND o.name ILIKE %(opponent)s
              AND s.name = %(season)s
        """),
    Query(
        id='top_carriers',
        description='Players who carried the ball furthest upfield in a season',
        params=('team', 'season', 'limit'),
        sql="""
            SELECT p.name AS player, count(*) AS carries,
                   sum(CASE WHEN c.is_progressive THEN 1 ELSE 0 END) AS progressive_carries,
                   round(sum(c.progression_m)) AS progression_m,
                   round(avg(c.distance_m), 1) AS avg_carry_m
            FROM carries c
            JOIN players p ON p.player_id = c.player_id
            JOIN teams t ON t.team_id = c.team_id
            JOIN matches m ON m.match_id = c.match_id
            JOIN seasons s ON s.season_id = m.season_id
            WHERE t.name ILIKE %(team)s AND s.name = %(season)s
            GROUP BY p.name
            ORDER BY progressive_carries DESC
            LIMIT %(limit)s
        """),
    Query(
        id='shots_in_match',
        description='Every shot in a match, with situation and body part',
        params=('opponent', 'season'),
        sql="""
            SELECT p.name AS player, t.name AS team, sh.minute, sh.outcome,
                   sh.is_big_chance, sh.is_penalty, sh.is_header,
                   sh.situation, round(sh.distance_m, 1) AS distance_m
            FROM shots sh
            JOIN matches m ON m.match_id = sh.match_id
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams t ON t.team_id = sh.team_id
            LEFT JOIN players p ON p.player_id = sh.player_id
            JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id
            WHERE s.name = %(season)s
              AND (h.name ILIKE %(opponent)s OR a.name ILIKE %(opponent)s)
            ORDER BY sh.minute
        """),
    Query(
        id='longest_sequences',
        description='The longest possessions of a season, by number of touches',
        params=('team', 'season', 'limit'),
        sql="""
            SELECT o.name AS opponent, m.match_date, ps.start_minute,
                   ps.events, ps.passes, ps.players,
                   round(ps.duration_s) AS duration_s,
                   ps.ended_in_shot, ps.ended_in_goal
            FROM possession_sequences ps
            JOIN matches m ON m.match_id = ps.match_id
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams t ON t.team_id = ps.team_id
            JOIN teams o ON o.team_id = CASE WHEN ps.team_id = m.home_team_id
                                             THEN m.away_team_id ELSE m.home_team_id END
            WHERE t.name ILIKE %(team)s AND s.name = %(season)s
            ORDER BY ps.events DESC
            LIMIT %(limit)s
        """),
    Query(
        id='results_by_venue',
        description='Home and away record for a team in a season',
        params=('team', 'season'),
        sql="""
            SELECT CASE WHEN r.is_home THEN 'home' ELSE 'away' END AS venue,
                   count(*) AS played,
                   sum(CASE WHEN r.result = 'W' THEN 1 ELSE 0 END) AS won,
                   sum(CASE WHEN r.result = 'D' THEN 1 ELSE 0 END) AS drawn,
                   sum(CASE WHEN r.result = 'L' THEN 1 ELSE 0 END) AS lost,
                   sum(r.goals) AS goals_for, sum(r.goals_conceded) AS goals_against
            FROM v_team_match_results r
            JOIN teams t ON t.team_id = r.team_id
            JOIN seasons s ON s.season_id = r.season_id
            WHERE t.name ILIKE %(team)s AND s.name = %(season)s
            GROUP BY r.is_home
            ORDER BY venue
        """),
    Query(
        id='biggest_wins',
        description='A team\'s largest victories in a season, by goal difference',
        params=('team', 'season', 'limit'),
        sql="""
            SELECT o.name AS opponent, r.match_date,
                   r.goals AS scored, r.goals_conceded AS conceded,
                   r.goal_difference, CASE WHEN r.is_home THEN 'H' ELSE 'A' END AS venue
            FROM v_team_match_results r
            JOIN teams t ON t.team_id = r.team_id
            JOIN teams o ON o.team_id = r.opponent_team_id
            JOIN seasons s ON s.season_id = r.season_id
            WHERE t.name ILIKE %(team)s AND s.name = %(season)s AND r.result = 'W'
            ORDER BY r.goal_difference DESC, r.goals DESC
            LIMIT %(limit)s
        """),
    Query(
        id='set_piece_shots',
        description='Shots from set pieces (corners, free kicks) for a team in a season',
        params=('team', 'season'),
        returns='row',
        sql="""
            SELECT count(*) AS set_piece_shots,
                   sum(CASE WHEN sh.is_goal THEN 1 ELSE 0 END) AS goals,
                   sum(CASE WHEN sh.is_header THEN 1 ELSE 0 END) AS headers
            FROM shots sh
            JOIN teams t ON t.team_id = sh.team_id
            JOIN matches m ON m.match_id = sh.match_id
            JOIN seasons s ON s.season_id = m.season_id
            WHERE t.name ILIKE %(team)s AND s.name = %(season)s
              AND sh.situation IN ('FromCorner', 'SetPiece', 'DirectFreekick',
                                   'ThrowinSetPiece')
        """),
]

REGISTRY: dict[str, Query] = {q.id: q for q in _QUERIES}


def normalise_season(value: Any) -> Any:
    """Map LLM season shorthand onto the warehouse form.

    Seasons are stored as `YYYY/YYYY` (e.g. `2025/2026`). Models routinely emit
    `2025/26` or `2025-26`, which match zero rows and look like an empty
    warehouse rather than a parameter mistake. Expand the two-digit end year
    before binding.
    """
    import re

    if not isinstance(value, str):
        return value
    text = value.strip()
    short = re.fullmatch(r'(\d{4})\s*[/-]\s*(\d{2})', text)
    if short:
        start, end2 = short.group(1), short.group(2)
        return f'{start}/{start[:2]}{end2}'
    full = re.fullmatch(r'(\d{4})\s*[/-]\s*(\d{4})', text)
    if full:
        return f'{full.group(1)}/{full.group(2)}'
    return value


def normalise_params(params: dict[str, Any]) -> dict[str, Any]:
    """Canonicalise parameter values the router is known to mistype."""
    out = dict(params)
    if 'season' in out:
        out['season'] = normalise_season(out['season'])
    return out


_CATALOGUE_PREAMBLE = (
    'Season parameters must use YYYY/YYYY (example: 2025/2026), not YYYY/YY.'
)


def describe(query_ids: Sequence[str] | None = None) -> str:
    """The catalogue the router chooses from, optionally narrowed.

    Showing all fifteen queries on every classify is most of the routing
    prompt, and the model only ever needs the handful a shortlist has already
    judged relevant. Unknown ids are dropped rather than raising: a narrowed
    catalogue is an optimisation, and a bad shortlist should cost recall, not
    the request.
    """
    if query_ids is None:
        chosen = list(_QUERIES)
    else:
        seen: set[str] = set()
        chosen = []
        for qid in query_ids:
            query = REGISTRY.get(qid)
            if query is not None and qid not in seen:
                seen.add(qid)
                chosen.append(query)
        if not chosen:
            chosen = list(_QUERIES)

    lines = [_CATALOGUE_PREAMBLE]
    for q in chosen:
        args = ', '.join(q.params) or 'none'
        lines.append(f'- {q.id}({args}): {q.description}')
    return '\n'.join(lines)


def describe_all() -> str:
    """Every allowlisted query. Prefer `describe(shortlist)` on the hot path."""
    return describe(None)


class QueryNotAllowed(ValueError):
    """The model asked for something outside the allowlist."""


def resolve_query_id(query_id: str) -> str | None:
    """Map a near-miss router id onto an allowlisted one, or None.

    DSPy occasionally invents a close cousin (`season_presses` for
    `season_pressing`). Accepting an unambiguous close match keeps the SQL
    path alive without opening the allowlist to free-form names.
    """
    import difflib

    if not query_id:
        return None
    if query_id in REGISTRY:
        return query_id
    matches = difflib.get_close_matches(query_id, REGISTRY.keys(), n=1, cutoff=0.82)
    return matches[0] if matches else None


def run(query_id: str, params: dict[str, Any] | None = None,
        schema: str | None = None) -> Evidence:
    """Execute one allowlisted query and return it with its provenance."""
    resolved = resolve_query_id(query_id)
    if resolved is None:
        raise QueryNotAllowed(
            f'{query_id!r} is not an allowlisted query. Available: '
            f'{", ".join(sorted(REGISTRY))}')
    query_id = resolved
    query = REGISTRY[query_id]

    params = normalise_params(dict(params or {}))
    missing = [p for p in query.params if p not in params]
    if missing:
        raise QueryNotAllowed(f'{query_id} needs parameter(s): {", ".join(missing)}')
    # Ignore anything extra rather than passing it to psycopg2.
    bound = {k: params[k] for k in query.params}

    # Belt and braces: the SQL in this file is fixed, but a future edit that
    # smuggled in a write should fail loudly rather than run.
    if not query.sql.strip().upper().startswith('SELECT'):
        raise QueryNotAllowed(f'{query_id} is not a SELECT')

    with connect(schema=schema or current_schema()) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute(query.sql, bound)
        rows = [dict(r) for r in cur.fetchall()]

    value: Any
    if query.returns == 'scalar':
        value = next(iter(rows[0].values())) if rows else None
    elif query.returns == 'row':
        value = rows[0] if rows else None
    else:
        value = rows

    return Evidence(query_id=query_id, params=bound, value=value,
                    description=query.description, unit=query.unit, rows=rows)
