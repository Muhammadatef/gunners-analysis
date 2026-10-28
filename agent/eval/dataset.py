#!/usr/bin/env python3
"""Phase 0: the evaluation dataset. Build this before any module.

    python -m agent.eval.dataset --build --season '2025/2026'
    python -m agent.eval.dataset --show 5

The master prompt is blunt about why: "Optimizers do nothing without labelled
examples and a metric. Skipping this is why most DSPy projects conclude the
framework is overhyped." An optimizer is a search over prompts guided by a
score; with no score there is nothing to search.

Two halves, deliberately different in how they are made.

**Statistical questions are generated, not written.** Each one is a template
instantiated against real rows, and its ground truth is produced by running the
same allowlisted query the agent would run. That makes the answer exact, makes
it free, and makes it regenerable after every new match without a human or a
token. It also scopes the measurement correctly: the eval tests whether the
agent routes to the right query and reports it faithfully, not whether the SQL
is right — the warehouse's own tests cover that.

**Tactical questions are hand-written**, because there is no SQL that produces
"did Arsenal press higher in the second half". They carry the evidence a good
answer must cite rather than a single correct string.

The split is by question *family*, not by row. Splitting randomly would put
"how many goals did Saka score" in train and "how many goals did Rice score" in
test, and a model that had memorised the first would score well on the second
for no good reason. Held-out families are ones the optimizer never sees in any
form.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.eval.tactical import TACTICAL_QUESTIONS  # noqa: E402
from agent.tools import sql_registry as registry    # noqa: E402
from db.connection import connect, current_schema   # noqa: E402

DATASET_PATH = os.path.join(ROOT, 'agent', 'eval', 'dataset.json')
SEED = 20260808          # fixed: the split must be reproducible


@dataclass
class Example:
    id: str
    question: str
    answer: str                       # rendered ground truth
    answer_value: Any                 # the comparable value
    answer_type: str                  # number | text | list | judgement
    family: str                       # question family, the unit of splitting
    intent: str                       # statistical | tactical | comparative
    expected_tool: str                # sql | retrieval | python
    expected_query: str | None = None
    query_params: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    split: str = 'train'


# ---------------------------------------------------------------------------
# statistical templates
# ---------------------------------------------------------------------------
# Each returns (question, answer_value, answer_text) given a fetched row.

def _f(value, nd=2):
    if value is None:
        return None
    return round(float(value), nd)


STAT_TEMPLATES = [
    # family, question template, query id, params builder, extractor
    dict(
        family='season_points',
        intent='statistical',
        question=lambda c: f"How many points did {c['team']} finish the {c['season']} season with?",
        query='season_summary',
        params=lambda c: {'team': c['team'], 'season': c['season']},
        extract=lambda v: (v['points'], f"{v['points']} points"),
    ),
    dict(
        family='season_record',
        intent='statistical',
        question=lambda c: f"What was {c['team']}'s win-draw-loss record in {c['season']}?",
        query='season_summary',
        params=lambda c: {'team': c['team'], 'season': c['season']},
        extract=lambda v: ([v['won'], v['drawn'], v['lost']],
                           f"{v['won']} wins, {v['drawn']} draws, {v['lost']} defeats"),
    ),
    dict(
        family='season_goals',
        intent='statistical',
        question=lambda c: f"How many goals did {c['team']} score in {c['season']}?",
        query='season_summary',
        params=lambda c: {'team': c['team'], 'season': c['season']},
        extract=lambda v: (v['goals_for'], f"{v['goals_for']} goals"),
    ),
    dict(
        family='season_conceded',
        intent='statistical',
        question=lambda c: f"How many goals did {c['team']} concede in {c['season']}?",
        query='season_summary',
        params=lambda c: {'team': c['team'], 'season': c['season']},
        extract=lambda v: (v['goals_against'], f"{v['goals_against']} conceded"),
    ),
    dict(
        family='season_ppda',
        intent='statistical',
        question=lambda c: f"What was {c['team']}'s average PPDA in {c['season']}?",
        query='season_pressing',
        params=lambda c: {'team': c['team'], 'season': c['season']},
        extract=lambda v: (_f(v['avg_ppda']), f"{_f(v['avg_ppda'])} PPDA"),
    ),
    dict(
        family='season_field_tilt',
        intent='statistical',
        question=lambda c: f"What was {c['team']}'s average field tilt in {c['season']}?",
        query='season_pressing',
        params=lambda c: {'team': c['team'], 'season': c['season']},
        extract=lambda v: (_f(v['avg_field_tilt']), f"{_f(v['avg_field_tilt'])}%"),
    ),
    dict(
        family='match_score',
        intent='statistical',
        question=lambda c: f"What was the score when {c['team']} played {c['opponent']} on {c['date']}?",
        query='match_result',
        params=lambda c: {'opponent': c['opponent'], 'season': c['season']},
        extract=None,          # filled by the match-specific builder
    ),
    dict(
        family='player_goals',
        intent='statistical',
        question=lambda c: f"How many goals did {c['player']} score in {c['season']}?",
        query='player_season_totals',
        params=lambda c: {'player': c['player'], 'season': c['season']},
        extract=lambda v: (v['goals'], f"{v['goals']} goals"),
    ),
    dict(
        family='player_assists',
        intent='statistical',
        question=lambda c: f"How many assists did {c['player']} provide in {c['season']}?",
        query='player_season_totals',
        params=lambda c: {'player': c['player'], 'season': c['season']},
        extract=lambda v: (v['assists'], f"{v['assists']} assists"),
    ),
    dict(
        family='player_minutes',
        intent='statistical',
        question=lambda c: f"How many minutes did {c['player']} play in {c['season']}?",
        query='player_season_totals',
        params=lambda c: {'player': c['player'], 'season': c['season']},
        extract=lambda v: (v['minutes'], f"{v['minutes']} minutes"),
    ),
    dict(
        family='player_prog_passes',
        intent='statistical',
        question=lambda c: f"How many progressive passes did {c['player']} make in {c['season']}?",
        query='player_season_totals',
        params=lambda c: {'player': c['player'], 'season': c['season']},
        extract=lambda v: (v['progressive_passes'],
                           f"{v['progressive_passes']} progressive passes"),
    ),
    dict(
        family='player_xt',
        intent='statistical',
        question=lambda c: f"What was {c['player']}'s expected threat per 90 in {c['season']}?",
        query='player_season_totals',
        params=lambda c: {'player': c['player'], 'season': c['season']},
        extract=lambda v: (_f(v['xt_per_90'], 3), f"{_f(v['xt_per_90'], 3)} xT per 90"),
    ),
]


def _team_context(season: str) -> dict:
    return {'team': 'Arsenal', 'season': season}


def build_statistical(season: str, schema: str) -> list[Example]:
    examples: list[Example] = []

    with connect(schema=schema) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.name FROM v_player_season_totals v
            JOIN players p ON p.player_id = v.player_id
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.team_name = 'Arsenal' AND s.name = %s AND v.minutes >= 600
            ORDER BY v.minutes DESC LIMIT 14
        """, (season,))
        players = [r[0] for r in cur.fetchall()]

        cur.execute("""
            SELECT o.name, m.match_date, m.home_score, m.away_score,
                   h.name, a.name
            FROM matches m
            JOIN seasons s ON s.season_id = m.season_id
            JOIN teams h ON h.team_id = m.home_team_id
            JOIN teams a ON a.team_id = m.away_team_id
            JOIN teams o ON o.team_id = CASE WHEN h.name = 'Arsenal'
                                             THEN m.away_team_id ELSE m.home_team_id END
            WHERE s.name = %s ORDER BY m.match_date
        """, (season,))
        matches = cur.fetchall()

    base = _team_context(season)

    # Team-level templates: one example each.
    for tpl in STAT_TEMPLATES:
        if tpl['extract'] is None or '{player}' in str(tpl['family']):
            continue
        if 'player' in tpl['family']:
            continue
        ctx = dict(base)
        evidence = registry.run(tpl['query'], tpl['params'](ctx), schema)
        if evidence.value is None:
            continue
        value, text = tpl['extract'](evidence.value)
        examples.append(Example(
            id=f"stat-{tpl['family']}",
            question=tpl['question'](ctx), answer=text, answer_value=value,
            answer_type='list' if isinstance(value, list) else 'number',
            family=tpl['family'], intent=tpl['intent'], expected_tool='sql',
            expected_query=tpl['query'], query_params=tpl['params'](ctx),
            evidence=[evidence.as_prompt()]))

    # Player templates: one per player.
    for tpl in STAT_TEMPLATES:
        if not tpl['family'].startswith('player_'):
            continue
        for player in players:
            ctx = dict(base, player=player)
            evidence = registry.run(tpl['query'], tpl['params'](ctx), schema)
            if evidence.value is None:
                continue
            value, text = tpl['extract'](evidence.value)
            if value is None:
                continue
            examples.append(Example(
                id=f"stat-{tpl['family']}-{player.lower().replace(' ', '-')}",
                question=tpl['question'](ctx), answer=text, answer_value=value,
                answer_type='number', family=tpl['family'], intent=tpl['intent'],
                expected_tool='sql', expected_query=tpl['query'],
                query_params=tpl['params'](ctx),
                evidence=[evidence.as_prompt()]))

    # Match scorelines: one per match.
    for opponent, date, hs, as_, home, away in matches:
        ctx = dict(base, opponent=opponent, date=date)
        arsenal_goals = hs if home == 'Arsenal' else as_
        opp_goals = as_ if home == 'Arsenal' else hs
        examples.append(Example(
            id=f'stat-match_score-{date}',
            question=(f"What was the score when Arsenal played {opponent} "
                      f"on {date}?"),
            answer=f'{home} {hs}-{as_} {away}',
            answer_value=[arsenal_goals, opp_goals],
            answer_type='list', family='match_score', intent='statistical',
            expected_tool='sql', expected_query='match_result',
            query_params={'opponent': opponent, 'season': season},
            evidence=[f'[match_result] {home} {hs}-{as_} {away} on {date}']))

    return examples


# ---------------------------------------------------------------------------
# assembly and splitting
# ---------------------------------------------------------------------------

# Held out entirely: the optimizer never sees a question from these families,
# in any instantiation. Chosen to span the intents rather than at random.
TEST_FAMILIES = {
    'player_assists',        # a statistical family with many instances
    'player_prog_passes',    # a derived per-player metric
    'season_field_tilt',     # a derived team metric
    'tactical_pressing',     # hand-written tactical judgement
    'tactical_buildup',
    'tactical_refusal',      # the refusals must be held out, or optimizing
                             # on them teaches the shape of the excuse
}

DEV_FAMILIES = {
    'player_minutes',
    'season_ppda',
    'tactical_comparison',
}

# `match_score` is deliberately in train despite being the largest family: 38
# near-identical questions in the test split would let a single routing habit
# dominate the score and drown out every other question type.


def assign_splits(examples: list[Example]) -> list[Example]:
    for ex in examples:
        if ex.family in TEST_FAMILIES:
            ex.split = 'test'
        elif ex.family in DEV_FAMILIES:
            ex.split = 'dev'
        else:
            ex.split = 'train'
    return examples


def build(season: str = '2025/2026', schema: str | None = None) -> list[Example]:
    schema = schema or current_schema()
    examples = build_statistical(season, schema)
    examples.extend(Example(**q) for q in TACTICAL_QUESTIONS)
    examples = assign_splits(examples)

    rng = random.Random(SEED)
    rng.shuffle(examples)
    return examples


def save(examples: list[Example], path: str = DATASET_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        json.dump([asdict(e) for e in examples], fh, indent=2, default=str)
    return path


def load(path: str = DATASET_PATH, split: str | None = None) -> list[Example]:
    with open(path) as fh:
        rows = json.load(fh)
    examples = [Example(**r) for r in rows]
    return [e for e in examples if split is None or e.split == split]


def summarise(examples: list[Example]) -> str:
    from collections import Counter
    splits = Counter(e.split for e in examples)
    intents = Counter(e.intent for e in examples)
    tools = Counter(e.expected_tool for e in examples)
    families = Counter(e.family for e in examples)
    lines = [f'{len(examples)} examples',
             f'  splits : ' + ', '.join(f'{k}={v}' for k, v in sorted(splits.items())),
             f'  intents: ' + ', '.join(f'{k}={v}' for k, v in sorted(intents.items())),
             f'  tools  : ' + ', '.join(f'{k}={v}' for k, v in sorted(tools.items())),
             f'  {len(families)} families']
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--build', action='store_true')
    ap.add_argument('--season', default='2025/2026')
    ap.add_argument('--show', type=int, default=0)
    ap.add_argument('--split', default=None)
    a = ap.parse_args(argv)

    if a.build:
        examples = build(a.season)
        path = save(examples)
        print(summarise(examples))
        print(f'\nwritten to {path}')
        return 0

    examples = load(split=a.split)
    print(summarise(examples))
    for ex in examples[:a.show]:
        print(f'\n[{ex.split}/{ex.intent}/{ex.expected_tool}] {ex.question}')
        print(f'   -> {ex.answer}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
