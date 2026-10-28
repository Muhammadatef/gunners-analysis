"""Match technical report generator — progressive, warehouse-first.

Pipeline:
  1. gather_evidence_pack  — match-scoped SQL (no request-time report cache)
  2. render_scoreboard     — deterministic MD streamed immediately
  3. write_analysis        — one short LM pass for the middle sections
  4. render_caveats        — deterministic caveats + hallucination gate

Scoreline numbers always come from the warehouse; the LM only decorates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = os.environ.get(
    'GUNNERS_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg2.extras import RealDictCursor  # noqa: E402

from agent.eval.metrics import hallucinated_numbers, numbers_in  # noqa: E402
from agent.tools import sql_registry  # noqa: E402
from db.connection import club_schema, connect  # noqa: E402
from visualizer import output_layout  # noqa: E402


REPORT_SECTIONS = (
    'Scoreline & context',
    'Defence',
    'Offence',
    'Key passes & progressive play',
    'Half-spaces & Zone 14',
    'Standouts',
    'Caveats',
)

# Scoreline + Caveats are template-rendered; LM only decorates the middle.
ANALYSIS_SECTIONS = (
    'Defence',
    'Offence',
    'Key passes & progressive play',
    'Half-spaces & Zone 14',
    'Standouts',
)

# Maps each analysis section to dashboard panel filenames (under panels/).
PANEL_SECTIONS: dict[str, list[str]] = {
    'Defence': [
        '04_home_defensive_block.png',
        '06_away_defensive_block.png',
        '05_gk_performance.png',
        '14_high_turnovers.png',
    ],
    'Offence': [
        '02_shot_map.png',
        '16_home_chance_creating.png',
        '18_away_chance_creating.png',
        '17_crosses_analysis.png',
    ],
    'Key passes & progressive play': [
        '01_home_pass_network.png',
        '03_away_pass_network.png',
        '07_home_progressive_passes.png',
        '09_away_progressive_passes.png',
        '13_home_pass_end_zones.png',
        '15_away_pass_end_zones.png',
    ],
    'Half-spaces & Zone 14': [
        '10_home_danger_zones.png',
        '12_away_danger_zones.png',
    ],
    'Standouts': [
        '08_match_momentum.png',
        '11_match_stats.png',
        '00_match_dashboard_unified.png',
    ],
}


def _section_signature():
    import dspy

    class WriteReportSection(dspy.Signature):
        """Write one short section of a post-match technical briefing.

        Use ONLY the evidence. Every number must appear in the evidence.
        Attribute every number to the team the evidence names for it.
        Two to four dense sentences or bullets. No markdown heading — the
        caller adds the ## title. No invented tactics or per-shot xG rankings.
        """

        section: str = dspy.InputField(desc='section title')
        opponent: str = dspy.InputField()
        season: str = dspy.InputField()
        evidence: str = dspy.InputField(desc='warehouse rows for this fixture')
        prose: str = dspy.OutputField(desc='2–4 sentences or bullets, no ## heading')

    return WriteReportSection


def _analysis_signature():
    import dspy

    class WriteMatchAnalysis(dspy.Signature):
        """Write the analysis half of a post-match report.

        Use ONLY the evidence. Every number must appear in the evidence.
        Attribute every number to the team the evidence names for it; xG and
        xGOT come from TEAM ATTRIBUTION and must never be swapped.
        Output Markdown with exactly these ## headings, in order:
        Defence, Offence, Key passes & progressive play,
        Half-spaces & Zone 14, Standouts.

        Ground claims in the labelled dashboard panels (defence blocks, shot
        map, progressive passes, danger zones / Zone 14 & half-spaces, etc.).
        Do NOT invent panel filenames — cite only panels listed in the evidence.
        Do NOT include Scoreline & context or Caveats — those are supplied
        separately. Keep the whole analysis under 400 words. Dense, specific,
        no padding, no invented per-shot xG rankings.
        """

        opponent: str = dspy.InputField()
        season: str = dspy.InputField()
        evidence: str = dspy.InputField(desc='warehouse rows + panel inventory')
        analysis: str = dspy.OutputField(
            desc='Markdown with the five analysis ## headings only')

    return WriteMatchAnalysis


# Back-compat — full-report signature kept for older imports / CLI docs.
def _write_signature():
    import dspy
    class WriteMatchReport(dspy.Signature):
        """Write a concise technical post-match report (legacy single-shot)."""

        opponent: str = dspy.InputField()
        season: str = dspy.InputField()
        evidence: str = dspy.InputField(desc='SQL rows and retrieved documents')
        report: str = dspy.OutputField(desc='Markdown report with required headings')
    return WriteMatchReport


WriteMatchReport = None


@dataclass
class EvidencePack:
    """Structured warehouse pack for one fixture — template + LM share this."""

    evidence_text: str
    numbers: set[float]
    query_ids: list[str]
    retrieved: list[dict]
    fixture: dict | None
    errors: list[str]
    team_stats: list[dict] = field(default_factory=list)
    tactical: list[dict] = field(default_factory=list)
    shots: list[dict] = field(default_factory=list)
    players: list[dict] = field(default_factory=list)
    panels: list[str] = field(default_factory=list)
    dashboard: str | None = None
    match_dir: str | None = None

    @property
    def match_id(self) -> int | None:
        if not self.fixture:
            return None
        mid = self.fixture.get('match_id')
        return int(mid) if mid is not None else None


@dataclass
class ReportResult:
    report: str
    match_id: int | None
    opponent: str
    season: str
    evidence: str
    evidence_numbers: set[float] = field(default_factory=set)
    unsupported_numbers: list[float] = field(default_factory=list)
    query_ids: list[str] = field(default_factory=list)
    retrieved: list[dict] = field(default_factory=list)
    latency_s: float = 0.0
    report_path: str | None = None
    errors: list[str] = field(default_factory=list)
    scoreboard: str = ''


def resolve_fixture(opponent: str, season: str, schema: str | None = None,
                    home: bool | None = None,
                    matchweek: int | None = None) -> dict | None:
    """Pick one Arsenal fixture vs opponent in season.

    When `matchweek` is set (e.g. GW 2), prefer that row — Leeds appears twice
    in a season (home and away).
    """
    season = sql_registry.normalise_season(season)
    opponent = normalise_opponent(opponent, schema=schema)
    if not opponent:
        return None
    opp = f'%{opponent}%'
    sql = """
        SELECT m.match_id, m.match_date, m.matchweek, m.competition,
               m.home_score, m.away_score,
               m.home_xg, m.away_xg, m.home_xgot, m.away_xgot,
               h.name AS home_team, a.name AS away_team,
               s.name AS season
        FROM matches m
        JOIN seasons s ON s.season_id = m.season_id
        JOIN teams h ON h.team_id = m.home_team_id
        JOIN teams a ON a.team_id = m.away_team_id
        WHERE s.name = %s
          AND (h.name ILIKE %s OR a.name ILIKE %s)
        ORDER BY m.match_date
    """
    with connect(schema=schema) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (season, opp, opp))
            rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return None
    if matchweek is not None:
        for r in rows:
            if r.get('matchweek') == matchweek:
                return r
    if home is True:
        for r in rows:
            if 'arsenal' in (r.get('home_team') or '').lower():
                return r
    if home is False:
        for r in rows:
            if 'arsenal' in (r.get('away_team') or '').lower():
                return r
    # Prefer Arsenal home if multiple; else first.
    for r in rows:
        if 'arsenal' in (r.get('home_team') or '').lower():
            return r
    return rows[0]


# WhoScored short names in the warehouse vs how people / LLMs say them.
_OPPONENT_ALIASES = {
    'manchester united': 'Man Utd',
    'man united': 'Man Utd',
    'man united fc': 'Man Utd',
    'manchester utd': 'Man Utd',
    'mufc': 'Man Utd',
    'manchester city': 'Man City',
    'man city': 'Man City',
    'mcfc': 'Man City',
    'tottenham hotspur': 'Tottenham',
    'tottenham': 'Tottenham',
    'spurs': 'Tottenham',
    'nottingham forest': 'Nottingham Forest',
    'nott\'m forest': 'Nottingham Forest',
    'forest': 'Nottingham Forest',
    'newcastle united': 'Newcastle',
    'newcastle': 'Newcastle',
    'west ham united': 'West Ham',
    'west ham': 'West Ham',
    'wolverhampton': 'Wolves',
    'wolverhampton wanderers': 'Wolves',
    'brighton and hove albion': 'Brighton',
    'brighton & hove albion': 'Brighton',
    'brighton': 'Brighton',
    'afc bournemouth': 'Bournemouth',
    'bournemouth': 'Bournemouth',
    'crystal palace': 'Crystal Palace',
    'leeds united': 'Leeds',
    'leeds': 'Leeds',
    'aston villa': 'Aston Villa',
    'villa': 'Aston Villa',
}


def _tokens(name: str) -> set[str]:
    noise = {'fc', 'afc', 'the', '&', 'and'}
    cleaned = name.lower().replace('&', ' ').replace('.', ' ').replace('-', ' ')
    return {t for t in cleaned.split() if t and t not in noise}


def normalise_opponent(name: str, schema: str | None = None) -> str:
    """Map LLM / vernacular club names onto warehouse `teams.name`.

    The event feed stores WhoScored short forms (`Man Utd`, not
    `Manchester United`). Slot extraction often emits the long form; without
    this bridge the fixture lookup returns nothing even when the match exists.
    """
    text = _clean_opponent(name)
    if not text:
        return ''
    key = text.lower().strip()
    if key in _OPPONENT_ALIASES:
        return _OPPONENT_ALIASES[key]

    team_names: list[str] = []
    try:
        with connect(schema=schema) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT name FROM teams ORDER BY name')
                team_names = [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return text

    for n in team_names:
        if n.lower() == key:
            return n
    for n in team_names:
        nl = n.lower()
        if key in nl or nl in key:
            return n

    wanted = _tokens(text)
    if not wanted:
        return text
    best, best_score = None, 0
    for n in team_names:
        if 'arsenal' in n.lower():
            continue
        overlap = len(wanted & _tokens(n))
        if overlap > best_score:
            best, best_score = n, overlap
    # Require at least one meaningful shared token (man↔man, leeds↔leeds).
    if best and best_score >= 1:
        return best
    return text


def _clean_opponent(name: str) -> str:
    """Strip GW / prose tails so 'Leeds GW 2 explain…' → 'Leeds'."""
    text = (name or '').strip()
    text = re.split(
        r'\s+(?:gw|g\.?w\.?|mw|matchweek|gameweek|explain|expalin|as|in)\b',
        text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r'\s+', ' ', text).strip(' .,;:')
    text = re.sub(r'\s+20\d{2}.*$', '', text).strip()
    return text


def gather_evidence(opponent: str, season: str, schema: str | None = None,
                    match_id: int | None = None, matchweek: int | None = None,
                    home: bool | None = None,
                    k_docs: int = 0) -> tuple[
                        str, set[float], list[str], list[dict], dict | None, list[str]]:
    """Back-compat tuple API — prefer `gather_evidence_pack`."""
    pack = gather_evidence_pack(
        opponent, season, schema=schema, match_id=match_id,
        matchweek=matchweek, home=home, k_docs=k_docs)
    return (pack.evidence_text, pack.numbers, pack.query_ids, pack.retrieved,
            pack.fixture, pack.errors)


def gather_evidence_pack(opponent: str, season: str, schema: str | None = None,
                         match_id: int | None = None, matchweek: int | None = None,
                         home: bool | None = None,
                         k_docs: int = 0) -> EvidencePack:
    """Deterministic evidence pack for one fixture (match-scoped SQL)."""
    season = sql_registry.normalise_season(season)
    opponent = normalise_opponent(opponent, schema=schema)
    errors: list[str] = []
    fixture = None
    if match_id:
        with connect(schema=schema) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT m.match_id, m.match_date, m.matchweek, m.competition,
                           m.home_score, m.away_score,
                           m.home_xg, m.away_xg, m.home_xgot, m.away_xgot,
                           h.name AS home_team, a.name AS away_team, s.name AS season
                    FROM matches m
                    JOIN seasons s ON s.season_id = m.season_id
                    JOIN teams h ON h.team_id = m.home_team_id
                    JOIN teams a ON a.team_id = m.away_team_id
                    WHERE m.match_id = %s
                """, (match_id,))
                row = cur.fetchone()
                fixture = dict(row) if row else None
        if fixture:
            ht, at = fixture['home_team'], fixture['away_team']
            opponent = at if 'arsenal' in (ht or '').lower() else ht
            season = fixture.get('season') or season
    else:
        fixture = resolve_fixture(
            opponent, season, schema=schema, matchweek=matchweek, home=home)

    if not fixture:
        return EvidencePack(
            evidence_text='No fixture found for that opponent and season.',
            numbers=set(), query_ids=[], retrieved=[], fixture=None,
            errors=['fixture_not_found'])

    mid = int(fixture['match_id'])
    parts: list[str] = [f'FIXTURE: {json.dumps(fixture, default=str)}',
                        team_attribution(fixture)]
    numbers: set[float] = numbers_in(parts[0])
    query_ids: list[str] = ['fixture_resolve']
    team_stats: list[dict] = []
    tactical: list[dict] = []
    shots: list[dict] = []
    players: list[dict] = []

    with connect(schema=schema) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for label, sql, sink in (
                ('match_team_stats', """
                    SELECT t.name AS team, ts.possession_pct, ts.field_tilt_pct,
                           ts.shots, ts.shots_on_target, ts.big_chances,
                           ts.passes, ts.passes_accurate, ts.progressive_passes,
                           ts.key_passes, ts.zone14_passes, ts.halfspace_passes,
                           ts.corners, ts.tackles, ts.interceptions, ts.xt,
                           ts.clearances, ts.aerials
                    FROM match_team_stats ts
                    JOIN teams t ON t.team_id = ts.team_id
                    WHERE ts.match_id = %s
                    ORDER BY ts.is_home DESC
                """, team_stats),
                ('match_tactical', """
                    SELECT t.name AS team, o.name AS opponent,
                           f.ppda, f.ppda_allowed, f.counterpresses, f.high_turnovers,
                           f.field_tilt_pct, f.sequences, f.avg_sequence_events,
                           f.direct_speed_ms, f.long_sequences, f.sequences_ending_in_shot
                    FROM match_tactical_features f
                    JOIN teams t ON t.team_id = f.team_id
                    JOIN teams o ON o.team_id = f.opponent_team_id
                    WHERE f.match_id = %s
                """, tactical),
                ('match_shots', """
                    SELECT p.name AS player, t.name AS team, sh.minute, sh.outcome,
                           sh.is_big_chance, sh.is_penalty, sh.is_header,
                           sh.situation, round(sh.distance_m, 1) AS distance_m
                    FROM shots sh
                    JOIN teams t ON t.team_id = sh.team_id
                    LEFT JOIN players p ON p.player_id = sh.player_id
                    WHERE sh.match_id = %s
                    ORDER BY sh.minute NULLS LAST
                    LIMIT 20
                """, shots),
                ('match_players', """
                    SELECT p.name AS player, t.name AS team,
                           ps.minutes_played, ps.goals, ps.assists, ps.shots,
                           ps.key_passes, ps.progressive_passes, ps.xt, ps.rating
                    FROM match_player_stats ps
                    JOIN players p ON p.player_id = ps.player_id
                    JOIN teams t ON t.team_id = ps.team_id
                    WHERE ps.match_id = %s AND ps.minutes_played >= 1
                    ORDER BY ps.xt DESC NULLS LAST
                    LIMIT 12
                """, players),
            ):
                try:
                    cur.execute(sql, (mid,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        sink.extend(rows)
                        blob = f'[{label}] {json.dumps(rows, default=str)}'
                        parts.append(blob)
                        numbers |= numbers_in(blob)
                        query_ids.append(label)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f'{label}: {exc}')

    retrieved: list[dict] = []
    if k_docs > 0:
        try:
            from agent.tools.retriever import PgVectorRetriever
            retriever = PgVectorRetriever(schema=schema)
            docs = retriever.search(
                f'{opponent} pressing shape build-up chances created '
                f'and conceded tactical analysis',
                k=k_docs,
                filters={'match_id': mid},
            )
            for d in docs:
                parts.append(d.as_prompt())
                numbers |= numbers_in(d.content)
                retrieved.append(d.as_dict())
        except Exception as exc:  # noqa: BLE001
            errors.append(f'retrieval: {exc}')

    parts.append(
        'NOTE: Per-shot xG is not populated in this warehouse; do not rank '
        'individual chances by xG.')
    pack = EvidencePack(
        evidence_text='\n\n'.join(parts),
        numbers=numbers,
        query_ids=query_ids,
        retrieved=retrieved,
        fixture=fixture,
        errors=errors,
        team_stats=team_stats,
        tactical=tactical,
        shots=shots,
        players=players,
    )
    return attach_panel_inventory(pack)


def attach_panel_inventory(pack: EvidencePack,
                           match_dir: str | Path | None = None) -> EvidencePack:
    """Enrich evidence with match.json panel list + Zone14 / half-space labels."""
    mid_dir = Path(match_dir) if match_dir else None
    if mid_dir is None and pack.fixture:
        mid_dir = _resolve_match_dir(pack.fixture)
    if mid_dir is None:
        return pack

    pack.match_dir = str(mid_dir)
    meta_path = mid_dir / 'match.json'
    panels: list[str] = []
    dashboard = None
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            panels = list(meta.get('panels') or [])
            dashboard = meta.get('dashboard')
        except Exception as exc:  # noqa: BLE001
            pack.errors.append(f'match.json: {exc}')
    if not panels:
        panels_dir = mid_dir / 'panels'
        if panels_dir.is_dir():
            panels = sorted(p.name for p in panels_dir.glob('*.png'))
        dash = mid_dir / '00_match_dashboard_unified.png'
        if dash.is_file():
            dashboard = dash.name
    pack.panels = panels
    pack.dashboard = dashboard

    # Explicit Zone 14 / half-space / defence labels from team_stats.
    zone_bits: list[str] = []
    for row in pack.team_stats:
        team = row.get('team') or '?'
        zone_bits.append(
            f"{team}: zone14_passes={row.get('zone14_passes')}, "
            f"halfspace_passes={row.get('halfspace_passes')}, "
            f"key_passes={row.get('key_passes')}, "
            f"progressive_passes={row.get('progressive_passes')}, "
            f"tackles={row.get('tackles')}, "
            f"interceptions={row.get('interceptions')}, "
            f"clearances={row.get('clearances')}")
    panel_lines = [
        'DASHBOARD_PANELS (open beside each claim):',
        f'  match_dir={mid_dir}',
        f'  dashboard={dashboard or "—"}',
    ]
    for section, files in PANEL_SECTIONS.items():
        available = [f for f in files if f in panels or f == dashboard
                     or (mid_dir / 'panels' / f).is_file()
                     or (mid_dir / f).is_file()]
        if not available and files:
            # still list expected panels so the LM knows the mapping
            available = files
        panel_lines.append(f'  [{section}] ' + ', '.join(
            f'panels/{f}' if not f.startswith('00_') else f for f in available))
    if zone_bits:
        panel_lines.append('ZONE14_HALFSPACE_DEFENCE: ' + ' | '.join(zone_bits))
    blob = '\n'.join(panel_lines)
    # Replace a previous inventory block rather than stacking duplicates.
    base = re.sub(
        r'\n*DASHBOARD_PANELS \(open beside each claim\):[\s\S]*$',
        '', pack.evidence_text.rstrip())
    pack.evidence_text = (base.rstrip() + '\n\n' + blob).strip()
    pack.numbers |= numbers_in(blob)
    if 'panel_inventory' not in pack.query_ids:
        pack.query_ids.append('panel_inventory')
    return pack


def _resolve_match_dir(fixture: dict) -> Path | None:
    try:
        season = fixture.get('season') or ''
        season_dir = output_layout.season_dir_name(season)
        root = output_layout.club_root()
        season_path = Path(root) / season_dir
        if not season_path.is_dir():
            return None
        home = fixture.get('home_team') or ''
        away = fixture.get('away_team') or ''
        date = str(fixture.get('match_date') or '')
        mw = fixture.get('matchweek')
        expected = output_layout.match_dir_name(date, home, away, mw)
        match_path = season_path / expected
        if match_path.is_dir():
            return match_path
        hs, as_ = output_layout.slugify(home), output_layout.slugify(away)
        for name in os.listdir(season_path):
            if hs in name and as_ in name and (season_path / name).is_dir():
                return season_path / name
    except Exception:  # noqa: BLE001
        return None
    return None


def dashboard_evidence_line(section: str, pack: EvidencePack) -> str:
    """Markdown citation line pointing at panel PNGs for a section."""
    wanted = PANEL_SECTIONS.get(section) or []
    links: list[str] = []
    for name in wanted:
        if name.startswith('00_'):
            rel = name
            exists = (
                pack.dashboard == name
                or (pack.match_dir and (Path(pack.match_dir) / name).is_file()))
        else:
            rel = f'panels/{name}'
            exists = (
                name in (pack.panels or [])
                or (pack.match_dir
                    and (Path(pack.match_dir) / 'panels' / name).is_file()))
        if exists or not pack.panels:
            links.append(f'[{rel}]({rel})')
    if not links:
        return ''
    return '**Dashboard evidence:** ' + ' · '.join(links)


def _fmt(v: Any, digits: int = 2) -> str:
    if v is None:
        return '—'
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
        return f'{f:.{digits}f}'
    except (TypeError, ValueError):
        return str(v)


def _row_for(rows: list[dict], team: str) -> dict | None:
    key = (team or '').lower()
    for r in rows:
        if key in str(r.get('team') or '').lower():
            return r
    return None


def team_attribution(fixture: dict) -> str:
    """Spell out which team owns each fixture-level number.

    The FIXTURE row only says home_xg / away_xg, and the writer swapped them
    (Bayern 2.46 v Arsenal 1.69 came out the other way round). Naming the team
    next to every value leaves nothing to map.
    """
    lines = ['TEAM ATTRIBUTION (use these exact pairings; never swap teams):']
    for side in ('home', 'away'):
        team = fixture.get(f'{side}_team') or side
        bits = [f'goals {fixture.get(f"{side}_score")}']
        for key in ('xg', 'xgot'):
            val = fixture.get(f'{side}_{key}')
            if val is not None:
                bits.append(f'{key.upper().replace("XGOT", "xGOT").replace("XG", "xG")} {val}')
        lines.append(f'- {team} ({side}): ' + ', '.join(bits))
    return '\n'.join(lines)


def _round_label(competition: str | None) -> str:
    return 'MD' if 'Champions' in (competition or '') else 'GW'


def render_scoreboard(pack: EvidencePack) -> str:
    """Deterministic ## Scoreline & context — numbers only from the warehouse."""
    fx = pack.fixture or {}
    home = fx.get('home_team') or 'Home'
    away = fx.get('away_team') or 'Away'
    hs, aws = fx.get('home_score'), fx.get('away_score')
    date = str(fx.get('match_date') or '')[:10]
    mw = fx.get('matchweek')
    lines = [
        f'# {home} {_fmt(hs)}–{_fmt(aws)} {away}',
        '',
        '## Scoreline & context',
        '',
        f'- **Date:** {date or "—"}'
        + (f' · **{_round_label(fx.get("competition"))} {mw}**' if mw is not None else ''),
        f'- **Score:** {home} {_fmt(hs)}–{_fmt(aws)} {away}',
    ]
    hxg, axg = fx.get('home_xg'), fx.get('away_xg')
    hxgot, axgot = fx.get('home_xgot'), fx.get('away_xgot')
    try:
        has_xg = not (
            (hxg is None and axg is None)
            or (
                float(hxg or 0) == 0 and float(axg or 0) == 0
                and float(hxgot or 0) == 0 and float(axgot or 0) == 0
            )
        )
    except (TypeError, ValueError):
        has_xg = hxg is not None or axg is not None
    if has_xg:
        lines.append(
            f'- **xG:** {home} {_fmt(hxg, 2)} · {away} {_fmt(axg, 2)}')
        lines.append(
            f'- **xGOT:** {home} {_fmt(hxgot, 2)} · {away} {_fmt(axgot, 2)}')
    for team in (home, away):
        st = _row_for(pack.team_stats, team)
        tac = _row_for(pack.tactical, team)
        bits = []
        if st:
            bits.append(f'poss {_fmt(st.get("possession_pct"), 1)}%')
            bits.append(
                f'shots {_fmt(st.get("shots"))}'
                f' ({_fmt(st.get("shots_on_target"))} on target)')
            if st.get('big_chances') is not None:
                bits.append(f'big chances {_fmt(st.get("big_chances"))}')
        if tac:
            bits.append(f'PPDA {_fmt(tac.get("ppda"), 2)}')
            bits.append(f'field tilt {_fmt(tac.get("field_tilt_pct"), 1)}%')
        if bits:
            lines.append(f'- **{team}:** ' + ' · '.join(bits))
    lines.append('')
    return '\n'.join(lines)


def render_caveats(pack: EvidencePack) -> str:
    """Deterministic caveats — no LM."""
    lines = ['## Caveats', '']
    fx = pack.fixture or {}
    try:
        team_xg_missing = (
            (fx.get('home_xg') is None and fx.get('away_xg') is None)
            or (
                float(fx.get('home_xg') or 0) == 0
                and float(fx.get('away_xg') or 0) == 0
                and float(fx.get('home_xgot') or 0) == 0
                and float(fx.get('away_xgot') or 0) == 0
            )
        )
    except (TypeError, ValueError):
        team_xg_missing = False
    has_shot_xg = any(s.get('xg') is not None for s in pack.shots)
    if team_xg_missing:
        lines.append(
            '- Team xG/xGOT were not available for this match (SofaScore feed '
            'had no expected-goals model); panels omit those metrics.')
    elif not has_shot_xg:
        lines.append(
            '- Per-shot xG is not populated in this warehouse; chance quality '
            'uses team xG/xGOT and shot counts, not individual shot models.')
    if pack.errors:
        lines.append(f'- Evidence gaps: {", ".join(pack.errors)}.')
    if len(lines) == 2:
        lines.append('- None beyond normal event-feed limitations.')
    lines.append('')
    return '\n'.join(lines)


def _evidence_for_lm(pack: EvidencePack, limit: int = 5500) -> str:
    text = pack.evidence_text
    if len(text) <= limit:
        return text
    return text[:limit] + '\n…[truncated]'


class MatchReportGenerator:
    """Progressive report: warehouse scoreboard + one LM analysis pass."""

    # Reports used to run with retrieval off, which meant the warehouse's
    # ~4,000 tactical documents were built, embedded, and then never read on
    # the product path. Four documents scoped to the fixture's own match_id is
    # one ANN query and no extra LM call — the cheapest quality the analyst
    # layer has. `GUNNERS_REPORT_K_DOCS=0` turns it back off.
    DEFAULT_K_DOCS = int(os.getenv('GUNNERS_REPORT_K_DOCS', '4'))

    def __init__(self, schema: str | None = None, k_docs: int | None = None):
        self.schema = schema
        self.k_docs = self.DEFAULT_K_DOCS if k_docs is None else k_docs
        self._analysis_writer = None

    def _writer(self):
        """Lazy Predict — must be built under the thread that owns DSPy settings."""
        if self._analysis_writer is None:
            import dspy
            self._analysis_writer = dspy.Predict(_analysis_signature())
        return self._analysis_writer

    def __call__(self, opponent: str, season: str, match_id: int | None = None,
                 matchweek: int | None = None, home: bool | None = None,
                 tier: str | None = None, match_dir: str | None = None):
        return self.forward(
            opponent=opponent, season=season, match_id=match_id,
            matchweek=matchweek, home=home, tier=tier, match_dir=match_dir)

    def prepare(self, opponent: str, season: str, match_id: int | None = None,
                matchweek: int | None = None, home: bool | None = None
                ) -> EvidencePack:
        return gather_evidence_pack(
            opponent, season, schema=self.schema, match_id=match_id,
            matchweek=matchweek, home=home, k_docs=self.k_docs)

    def write_analysis(self, pack: EvidencePack, opponent: str, season: str,
                       tier: str | None = None) -> str:
        """Single LM call for the five analysis sections (wall-time friendly)."""
        from agent import config

        evidence = _evidence_for_lm(pack)
        season_n = sql_registry.normalise_season(season)
        opp = opponent
        if pack.fixture:
            ht = pack.fixture.get('home_team') or ''
            at = pack.fixture.get('away_team') or ''
            opp = at if 'arsenal' in ht.lower() else ht
        use_tier = tier or config.DEFAULT_TIER
        with config.use_tier(use_tier, max_tokens=1800):
            pred = self._writer()(
                opponent=opp, season=season_n, evidence=evidence)
        return (getattr(pred, 'analysis', '') or '').strip()

    def write_sections(self, pack: EvidencePack, opponent: str, season: str,
                       tier: str | None = None) -> dict[str, str]:
        """Parse a single analysis write into per-section bodies (ordered)."""
        analysis = self.write_analysis(pack, opponent, season, tier=tier)
        return _split_analysis_sections(analysis)

    def assemble(self, pack: EvidencePack, sections: dict[str, str],
                 opponent: str, season: str, started: float,
                 analysis_md: str | None = None,
                 match_dir: str | None = None) -> ReportResult:
        if match_dir:
            pack = attach_panel_inventory(pack, match_dir)
        scoreboard = render_scoreboard(pack)
        caveats = render_caveats(pack)
        if analysis_md and analysis_md.strip():
            middle = _inject_dashboard_citations(analysis_md.strip(), pack)
        else:
            parts_m = []
            for title in ANALYSIS_SECTIONS:
                prose = (sections.get(title) or '').strip()
                parts_m.append(f'## {title}')
                parts_m.append('')
                parts_m.append(
                    prose or '_No analysis generated for this section._')
                cite = dashboard_evidence_line(title, pack)
                if cite:
                    parts_m.append('')
                    parts_m.append(cite)
                parts_m.append('')
            middle = '\n'.join(parts_m).strip()
        report = (
            scoreboard.rstrip() + '\n\n' + middle + '\n\n' + caveats.rstrip()
            + '\n')
        unsupported = sorted(hallucinated_numbers(report, pack.numbers))
        path = None
        if pack.fixture:
            path = _maybe_write_report(
                pack.fixture, report, match_dir=pack.match_dir or match_dir)
        return ReportResult(
            report=report,
            match_id=pack.match_id,
            opponent=opponent,
            season=sql_registry.normalise_season(season),
            evidence=pack.evidence_text,
            evidence_numbers=pack.numbers,
            unsupported_numbers=unsupported,
            query_ids=pack.query_ids,
            retrieved=pack.retrieved,
            latency_s=round(time.time() - started, 3),
            report_path=path,
            errors=pack.errors,
            scoreboard=scoreboard,
        )

    def forward(self, opponent: str, season: str, match_id: int | None = None,
                matchweek: int | None = None, home: bool | None = None,
                tier: str | None = None, match_dir: str | None = None):
        started = time.time()
        pack = self.prepare(
            opponent, season, match_id=match_id, matchweek=matchweek, home=home)
        if match_dir:
            pack = attach_panel_inventory(pack, match_dir)
        if pack.fixture is None:
            hint = opponent or '(unknown opponent)'
            msg = (
                f'No evidence was found for that fixture ({hint}, '
                f'{sql_registry.normalise_season(season)}'
                f'{f", GW{matchweek}" if matchweek else ""}). '
                'Name the opponent and season clearly '
                '(e.g. Arsenal vs Leeds, GW 2, 2025/2026).')
            return ReportResult(
                report=msg, match_id=None, opponent=opponent, season=season,
                evidence=pack.evidence_text, unsupported_numbers=[],
                query_ids=pack.query_ids, retrieved=pack.retrieved,
                latency_s=round(time.time() - started, 3),
                errors=pack.errors or ['fixture_not_found'],
                scoreboard='')
        analysis = self.write_analysis(pack, opponent, season, tier=tier)
        sections = _split_analysis_sections(analysis)
        return self.assemble(
            pack, sections, opponent, season, started, analysis_md=analysis,
            match_dir=match_dir)


def _inject_dashboard_citations(analysis: str, pack: EvidencePack) -> str:
    """Ensure each analysis ## section ends with a Dashboard evidence line."""
    sections = _split_analysis_sections(analysis)
    parts: list[str] = []
    for title in ANALYSIS_SECTIONS:
        body = (sections.get(title) or '').strip()
        # Strip any LM-emitted citation so we control the links.
        body = re.sub(
            r'(?im)^\*\*Dashboard evidence:\*\*.*$', '', body).strip()
        parts.append(f'## {title}')
        parts.append('')
        parts.append(body or '_No analysis generated for this section._')
        cite = dashboard_evidence_line(title, pack)
        if cite:
            parts.append('')
            parts.append(cite)
        parts.append('')
    return '\n'.join(parts).strip()


def _split_analysis_sections(analysis: str) -> dict[str, str]:
    """Best-effort split of LM analysis MD into section bodies."""
    text = (analysis or '').strip()
    out = {s: '' for s in ANALYSIS_SECTIONS}
    if not text:
        return out
    # Split on ## headings
    parts = re.split(r'(?m)^##\s+', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.splitlines()
        title = lines[0].strip()
        body = '\n'.join(lines[1:]).strip()
        for known in ANALYSIS_SECTIONS:
            if title.lower().startswith(known.lower()):
                out[known] = body
                break
    return out


def _maybe_write_report(fixture: dict, report: str,
                        match_dir: str | Path | None = None) -> str | None:
    """Write REPORT.md beside the rendered match dashboard when we can find it."""
    try:
        if match_dir:
            match_path = Path(match_dir)
            if not match_path.is_dir():
                match_path.mkdir(parents=True, exist_ok=True)
        else:
            resolved = _resolve_match_dir(fixture)
            if resolved is None:
                return None
            match_path = resolved
        out = match_path / 'REPORT.md'
        out.write_text(report.strip() + '\n', encoding='utf-8')
        return str(out)
    except Exception:  # noqa: BLE001
        return None


def generate_match_report(
        match_id: int,
        match_dir: str,
        season: str,
        *,
        opponent: str | None = None,
        tier: str | None = None,
        schema: str | None = None,
        club: str | None = None,
        generator: MatchReportGenerator | None = None) -> str:
    """Write panel-grounded REPORT.md into match_dir; return path.

    Used by the season DAG (via the analyst HTTP service) and the CLI.
    Pass `generator` when calling from a FastAPI worker thread so the
    DSPy Predict was constructed on the event-loop thread.
    """
    use_tier = tier or os.environ.get('GUNNERS_REPORT_TIER') or 'fallback'
    if club and not schema:
        schema = club_schema(club)
    # Do not call config.configure() here — the analyst lifespan already
    # configured DSPy on the main thread; reconfigure from a worker thread
    # raises. MatchReportGenerator.write_analysis uses config.use_tier.

    opp = opponent or ''
    if not opp:
        # Resolve from warehouse so callers only need match_id + match_dir.
        with connect(schema=schema) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT h.name AS home_team, a.name AS away_team, s.name AS season
                    FROM matches m
                    JOIN seasons s ON s.season_id = m.season_id
                    JOIN teams h ON h.team_id = m.home_team_id
                    JOIN teams a ON a.team_id = m.away_team_id
                    WHERE m.match_id = %s
                """, (match_id,))
                row = cur.fetchone()
        if not row:
            raise ValueError(f'match_id {match_id} not found in warehouse')
        ht, at = row['home_team'], row['away_team']
        opp = at if 'arsenal' in (ht or '').lower() else ht
        season = row.get('season') or season

    gen = generator or MatchReportGenerator(schema=schema)
    result = gen.forward(
        opponent=opp, season=season, match_id=match_id,
        tier=use_tier, match_dir=match_dir)
    if result.errors and 'fixture_not_found' in result.errors:
        raise ValueError(result.report)
    path = result.report_path
    if not path:
        # Force write even if fixture layout lookup failed.
        path = str(Path(match_dir) / 'REPORT.md')
        Path(path).write_text(result.report.strip() + '\n', encoding='utf-8')
    return path


# ---------------------------------------------------------------------------
# LLM understanding of report requests (not regex)
# ---------------------------------------------------------------------------

_REPORT_HINT = re.compile(
    r'\b(report|briefing|write[- ]?up|technical analysis|'
    r'analyse the match|analyze the match|post[- ]?match)\b',
    re.I,
)
_GW_RE = re.compile(
    r'\b(?:gw|g\.?w\.?|mw|matchweek|gameweek)\s*(\d{1,2})\b', re.I)
_SEASON_RE = re.compile(
    r'\b(20\d{2})\s*[/-]\s*(\d{2}|\d{4})\b')


def looks_like_report_request_fast(question: str) -> bool:
    """Cheap lexical gate — full NLU still used when this is inconclusive."""
    return bool(_REPORT_HINT.search(question or ''))


def try_parse_report_slots_fast(
        question: str, season_default: str = '2025/2026') -> dict | None:
    """Deterministic slots when opponent + season/GW are explicit in the text.

    Returns None when the opponent cannot be resolved — caller should fall
    through to the DSPy extractor.
    """
    q = question or ''
    season = sql_registry.normalise_season(season_default)
    m_season = _SEASON_RE.search(q)
    if m_season:
        season = sql_registry.normalise_season(
            f'{m_season.group(1)}/{m_season.group(2)}')
    matchweek = None
    m_gw = _GW_RE.search(q)
    if m_gw:
        matchweek = int(m_gw.group(1))

    opponent = ''
    m_vs = re.search(
        r'\b(?:vs\.?|versus|against)\s+([A-Za-z][A-Za-z .]+?)(?='
        r'\s+(?:gw|g\.?w\.?|mw|matchweek|gameweek|20\d{2}|explain|as|,|\.|$))',
        q, flags=re.I)
    if m_vs:
        opponent = normalise_opponent(_clean_opponent(m_vs.group(1)))
    if not opponent:
        lowered = q.lower()
        best = ''
        for alias, canon in sorted(
                _OPPONENT_ALIASES.items(), key=lambda kv: -len(kv[0])):
            if alias in lowered and len(alias) > len(best):
                best = alias
                opponent = canon
        if not opponent:
            for name in ('Man Utd', 'Man City', 'Tottenham', 'Newcastle',
                         "Nott'm Forest", 'Leeds', 'West Ham', 'Brighton',
                         'Crystal Palace', 'Aston Villa', 'Wolves', 'Fulham',
                         'Brentford', 'Bournemouth', 'Everton', 'Chelsea',
                         'Liverpool', 'Sunderland'):
                if name.lower() in lowered:
                    opponent = name
                    break

    if not opponent:
        return None
    home = None
    if re.search(r'\b(at home|home game|emirates)\b', q, re.I):
        home = True
    elif re.search(r'\b(away|at old trafford|on the road)\b', q, re.I):
        home = False
    return {
        'opponent': opponent,
        'season': season,
        'matchweek': matchweek,
        'home': home,
        'reasoning': 'Parsed fixture slots from the question text',
    }


def _extract_report_signature():
    import dspy

    class ExtractReportSlots(dspy.Signature):
        """Identify which Arsenal fixture a match-report request refers to.

        Arsenal is always the club of interest. Name the opponent using the
        short WhoScored-style club name when you know it: Man Utd, Man City,
        Tottenham, Newcastle, West Ham, Wolves, Brighton, Bournemouth, Leeds,
        Nottingham Forest, Crystal Palace, Aston Villa, Liverpool, Chelsea,
        Everton, Fulham, Brentford, Burnley, Sunderland. Prefer those over
        long forms like "Manchester United".
        Season must be YYYY/YYYY (expand 2025/26 → 2025/2026). If the user
        says GW / matchweek / gameweek N, put that integer in matchweek;
        otherwise leave matchweek empty. home_away is home, away, or empty
        when not stated. Do not invent an opponent — if unclear, leave
        opponent empty.
        """

        question: str = dspy.InputField()
        default_season: str = dspy.InputField(
            desc='season to use when the question does not name one')
        opponent: str = dspy.OutputField(desc='opponent club name, or empty')
        season: str = dspy.OutputField(desc='YYYY/YYYY')
        matchweek: str = dspy.OutputField(desc='integer as text, or empty')
        home_away: str = dspy.OutputField(desc='home | away | empty')
        reasoning: str = dspy.OutputField(desc='one sentence')

    return ExtractReportSlots


def _slots_look_broken(slots: dict) -> bool:
    """Adapter junk or empty opponent when the ask clearly named a club."""
    opp = (slots.get('opponent') or '').strip()
    reason = (slots.get('reasoning') or '').strip()
    if opp.upper().replace(' ', '-') in {
            'SINGLE-RESULT-END', 'MULTI-RESULT-END', 'COMPLETED'}:
        return True
    if 'RESULT-END' in opp.upper():
        return True
    # Empty reasoning + empty opponent is typical of a failed JSON parse.
    if not opp and not reason:
        return True
    return False


class ReportSlotExtractor:
    """DSPy module: natural language → fixture slots for the report skill."""

    def __init__(self):
        import dspy
        self.extract = dspy.ChainOfThought(_extract_report_signature())

    def __call__(self, question: str, season_default: str = '2025/2026') -> dict:
        return self.forward(question, season_default)

    def forward(self, question: str, season_default: str = '2025/2026') -> dict:
        slots = self._parse_prediction(self.extract(
            question=question,
            default_season=sql_registry.normalise_season(season_default),
        ), season_default)
        if not _slots_look_broken(slots):
            return slots

        from agent import config
        import dspy as _dspy

        current = getattr(getattr(_dspy.settings, 'lm', None), 'model', '') or ''
        if config.TIERS['fallback'][0] in current:
            return slots
        with config.use_tier('fallback'):
            return self._parse_prediction(self.extract(
                question=question,
                default_season=sql_registry.normalise_season(season_default),
            ), season_default)

    def _parse_prediction(self, pred, season_default: str) -> dict:
        season = sql_registry.normalise_season(
            (getattr(pred, 'season', '') or season_default).strip())
        opponent = (getattr(pred, 'opponent', '') or '').strip()
        # Model sometimes echoes "Arsenal vs Leeds" — keep the non-Arsenal side.
        if ' vs ' in opponent.lower() or ' versus ' in opponent.lower():
            parts = re.split(r'\s+(?:vs\.?|versus)\s+', opponent, flags=re.I)
            opponent = next(
                (p.strip() for p in parts if 'arsenal' not in p.lower()),
                opponent)
        opponent = normalise_opponent(opponent)
        mw_raw = (getattr(pred, 'matchweek', '') or '').strip()
        matchweek = None
        if mw_raw.isdigit():
            matchweek = int(mw_raw)
        home_away = (getattr(pred, 'home_away', '') or '').strip().lower()
        home = None
        if home_away in ('home', 'h'):
            home = True
        elif home_away in ('away', 'a'):
            home = False
        return {
            'opponent': opponent,
            'season': season,
            'matchweek': matchweek,
            'home': home,
            'reasoning': getattr(pred, 'reasoning', '') or '',
        }


def classify_report_intent(question: str) -> tuple[bool, str]:
    """Use the shared IntentClassifier — True when tool == report."""
    from agent.modules import IntentClassifier
    route = IntentClassifier()(question=question)
    tool = (getattr(route, 'tool', '') or '').strip().lower()
    reasoning = getattr(route, 'reasoning', '') or ''
    return tool == 'report', reasoning


def looks_like_report_request(question: str) -> bool:
    """LLM intent check (kept name for callers). Requires configured DSPy LM."""
    try:
        is_report, _ = classify_report_intent(question)
        return is_report
    except Exception:  # noqa: BLE001
        return False


def parse_report_slots(question: str, season_default: str = '2025/2026') -> dict:
    """Fixture slots for a report ask. Fast path first; DSPy only if needed."""
    fast = try_parse_report_slots_fast(question, season_default)
    if fast and fast.get('opponent'):
        return fast
    return ReportSlotExtractor()(question, season_default=season_default)


def main(argv=None) -> int:
    from agent import config

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--opponent', default='')
    ap.add_argument('--season', default='2025/2026')
    ap.add_argument('--match-id', type=int, default=None)
    ap.add_argument('--match-dir', default=None,
                    help='Force REPORT.md into this match folder')
    ap.add_argument('--matchweek', type=int, default=None)
    ap.add_argument('--club', default=None)
    ap.add_argument('--tier', default=config.DEFAULT_TIER, choices=sorted(config.TIERS))
    ap.add_argument('--no-mlflow', action='store_true')
    a = ap.parse_args(argv)

    if a.match_id and a.match_dir:
        path = generate_match_report(
            a.match_id, a.match_dir, a.season,
            opponent=a.opponent or None, tier=a.tier, club=a.club)
        print(f'wrote {path}')
        return 0

    if not a.opponent and not a.match_id:
        ap.error('--opponent or --match-id is required')

    config.configure(a.tier, trace=not a.no_mlflow)
    schema = club_schema(a.club) if a.club else None
    gen = MatchReportGenerator(schema=schema)
    result = gen(opponent=a.opponent or 'Arsenal', season=a.season,
                 match_id=a.match_id, matchweek=a.matchweek, tier=a.tier,
                 match_dir=a.match_dir)

    print(result.report)
    print('-' * 60)
    print(f'match_id   {result.match_id}')
    print(f'queries    {", ".join(result.query_ids)}')
    print(f'latency    {result.latency_s}s')
    if result.report_path:
        print(f'wrote      {result.report_path}')
    if result.unsupported_numbers:
        print(f'hallucinated numbers: {result.unsupported_numbers}')
    return 0 if not result.errors else 1


if __name__ == '__main__':
    sys.exit(main())
