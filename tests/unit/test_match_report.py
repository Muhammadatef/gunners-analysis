"""MatchReport skill — warehouse resolution + hallucination gate (no LLM).

Intent / slot understanding is DSPy (`IntentClassifier`, `ReportSlotExtractor`)
and needs a live LM; these unit tests cover the deterministic half only.
"""

from __future__ import annotations

from agent.eval.metrics import hallucinated_numbers, numbers_in
from agent.report import (
    REPORT_SECTIONS,
    _clean_opponent,
    normalise_opponent,
    render_caveats,
    render_scoreboard,
    try_parse_report_slots_fast,
    EvidencePack,
)
from agent.tools import sql_registry


def test_normalise_season_short_form():
    assert sql_registry.normalise_season('2025/26') == '2025/2026'
    assert sql_registry.normalise_season('2025/2026') == '2025/2026'
    assert sql_registry.normalise_season('2025-2026') == '2025/2026'


def test_clean_opponent_strips_noise_tails():
    assert _clean_opponent('Leeds GW 2 explain the match') == 'Leeds'
    assert _clean_opponent('Manchester City') == 'Manchester City'


def test_normalise_opponent_man_united_aliases():
    assert normalise_opponent('Man United') == 'Man Utd'
    assert normalise_opponent('Manchester United') == 'Man Utd'
    assert normalise_opponent('Man City') == 'Man City'
    assert normalise_opponent('Manchester City') == 'Man City'
    assert normalise_opponent('Spurs') == 'Tottenham'


def test_fast_slots_man_utd_gw1():
    slots = try_parse_report_slots_fast(
        'generate a report of Arsenal vs Man Utd GW 1 2025-2026')
    assert slots is not None
    assert slots['opponent'] == 'Man Utd'
    assert slots['matchweek'] == 1
    assert slots['season'] == '2025/2026'


def test_report_sections_outline():
    assert 'Scoreline & context' in REPORT_SECTIONS
    assert 'Caveats' in REPORT_SECTIONS
    assert len(REPORT_SECTIONS) == 7


def test_scoreboard_template_uses_fixture_numbers():
    pack = EvidencePack(
        evidence_text='FIXTURE',
        numbers={0, 1, 1.52, 1.31},
        query_ids=['fixture_resolve'],
        retrieved=[],
        fixture={
            'match_id': 1903125,
            'match_date': '2025-08-17',
            'matchweek': 1,
            'home_team': 'Man Utd',
            'away_team': 'Arsenal',
            'home_score': 0,
            'away_score': 1,
            'home_xg': 1.52,
            'away_xg': 1.31,
            'home_xgot': 0.4,
            'away_xgot': 0.9,
        },
        errors=[],
        team_stats=[
            {'team': 'Man Utd', 'possession_pct': 48.2, 'shots': 11,
             'shots_on_target': 3, 'big_chances': 2},
            {'team': 'Arsenal', 'possession_pct': 51.8, 'shots': 9,
             'shots_on_target': 4, 'big_chances': 1},
        ],
        tactical=[
            {'team': 'Arsenal', 'ppda': 9.1, 'field_tilt_pct': 55.0},
        ],
    )
    md = render_scoreboard(pack)
    assert '## Scoreline & context' in md
    assert 'Man Utd 0–1 Arsenal' in md or '0–1' in md
    assert '1.52' in md and '1.31' in md
    assert 'GW 1' in md
    caveats = render_caveats(pack)
    assert '## Caveats' in caveats
    assert 'Per-shot xG' in caveats


def test_hallucination_gate_on_sample_evidence():
    evidence = 'FIXTURE: home_score 2 away_score 1 home_xg 1.84'
    nums = numbers_in(evidence)
    good = '## Scoreline & context\nArsenal scored 2 and conceded 1. xG was 1.84.\n'
    assert hallucinated_numbers(good, nums) == set()
    bad = '## Scoreline & context\nArsenal scored 5 with 3.2 xG.\n'
    unsupported = hallucinated_numbers(bad, nums)
    assert 5.0 in unsupported or 3.2 in unsupported
