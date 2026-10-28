#!/usr/bin/env python3
"""Left-flank player analysis — Athletic-style player boards.

Canonical layout: an Athletic-style single-player board.
Cream canvas, teal passes, red heat, captions *below* the pitch, numbered radars.

    python visualizer/player_performance.py --player-id 399296 --partner-id 386519
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use('Agg')

import matplotlib.image as mpimg
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, to_rgba
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec
from matplotlib.patches import FancyArrowPatch, Rectangle
from mplsoccer import Pitch, Radar
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import connect  # noqa: E402

warnings.filterwarnings('ignore')

# --- Athletic / Ødegaard board vocabulary ---------------------------------
bg_color = '#F6F3EE'
line_color = '#1F1F1F'
pitch_line = '#3A3A3A'
path_eff = [path_effects.Stroke(linewidth=2.2, foreground=bg_color), path_effects.Normal()]
ARSENAL_RED = '#C8102E'
ARSENAL_GOLD = '#9C824A'
TEAL = '#1D8A7A'
TEAL_DARK = '#145E54'
NAVY = '#1B3A4B'
INK = '#2C2C2C'
MUTED = '#5C5C5C'
AUTHOR = '@maf74__'
AUTHOR_LINK = 'linkedin.com/in/mohamed-atef-fahmy-khalil'
CREDIT = 'Data: WhoScored / Opta warehouse  |  Built with mplsoccer'
ASSETS = ROOT / 'visualizer' / 'assets'

TZOLIS_ID = 399296
CALAFIORI_ID = 386519
ARSENAL_TEAM_ID = 13

# X/Twitter: RGBA PNGs and oversized canvases often fail upload. Archive PNGs
# are flattened to RGB; sibling *_x.jpg copies stay under platform caps.
# Re-exported from social_export so callers passing an explicit
# social_long_edge keep working; the export itself lives there now.
from visualizer.social_export import (            # noqa: E402
    PANEL_LONG_EDGE as SOCIAL_LONG_EDGE_PANEL,
    UNIFIED_LONG_EDGE as SOCIAL_LONG_EDGE_UNIFIED,
    JPEG_QUALITY as SOCIAL_JPEG_QUALITY,
)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or '#FFFFFF').lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def save_figure(fig, path: Path | str, *, dpi: int, bg: str = bg_color,
                social_long_edge: int | None = None) -> Path:
    """Write RGB PNG (no alpha) plus an upload-safe JPEG sibling.

    This used to carry its own copy of the export: flatten the alpha, scale to
    a long edge, encode. Matching long edges was never the hard part. The copy
    had no ratio clamp, no pixel box and no byte ceiling, so a player card
    could be over LinkedIn's 4320-pixel height limit and nothing would say so -
    LinkedIn accepts such an upload and then renders nothing at all.

    `social_export` is the one place those limits are written down and proved,
    so this delegates rather than tracking them by hand in two files.
    """
    from visualizer.social_export import export_social_jpg, make_png_upload_safe

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor=bg, dpi=dpi)
    buf.seek(0)
    rgba = Image.open(buf).convert('RGBA')
    rgb = Image.new('RGB', rgba.size, _hex_to_rgb(bg))
    rgb.paste(rgba, mask=rgba.split()[3])
    rgb.save(path, format='PNG', optimize=True)
    make_png_upload_safe(path, bg)

    name = path.name.lower()
    unified = social_long_edge == SOCIAL_LONG_EDGE_UNIFIED or (
        social_long_edge is None
        and (name.startswith('00_') or 'unified' in name))
    social = export_social_jpg(path, bg=bg, unified=unified)
    with Image.open(social) as im:
        print(f'    + {social.name} ({im.size[0]}x{im.size[1]})')
    return path

# Warehouse events are UEFA metres (105 × 68), same as match_report panels.
# Opta/WhoScored: y=0 is the *right* touchline (camera side), y=68 is the
# *left* touchline — so the left flank is high-y.
LEFT_Y = 34.0          # left half
LEFT_CORRIDOR_Y = 45.33  # left channel / half-space band

# Zone 14 + half-spaces — same geometry as match_report.draw_pass_map
ZONE14 = (70.0, 88.54, 22.66, 45.32)          # x0,x1,y0,y1
HALFSPACE_L = (70.0, 105.0, 45.32, 56.95)     # attacking left half-space
HALFSPACE_R = (70.0, 105.0, 11.33, 22.66)     # attacking right half-space

CMAP_HEAT = LinearSegmentedColormap.from_list(
    'athletic_heat', ['#F6F3EE', '#F4C7B0', '#E07A3D', '#C8102E', '#7A0C10'], N=256)
COL_ZONE14 = '#E07A3D'
COL_HALFSPACE = TEAL
COL_TZOLIS = TEAL
COL_CALA = NAVY

# Eight spokes max — Athletic density. Values printed on the chart + table.
RADAR_METRICS = [
    ('Rating', 'rating', False),
    ('Touches', 'touches', False),
    ('Pass %', 'pass_pct', False),
    ('Prog. passes', 'progressive_passes', False),
    ('Key passes', 'key_passes', False),
    ('Take-ons', 'take_ons', False),
    ('Def. actions', 'tackles_won', False),
    ('Left-touch %', 'left_pct', False),
]


@dataclass
class PlayerMatch:
    match_id: int
    date: str
    competition: str
    matchweek: int | None
    opponent: str
    score: str
    short: str
    label: str
    stats: dict[str, Any]
    events: pd.DataFrame
    shirt: int | None = None


@dataclass
class Bundle:
    player: dict
    partner: dict
    player_apps: list[PlayerMatch]
    partner_apps: list[PlayerMatch]
    links: dict[int, pd.DataFrame] = field(default_factory=dict)  # match_id → links


def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def _pitch(ax):
    """Horizontal UEFA pitch — Athletic board: cream grass, thin lines."""
    p = Pitch(
        pitch_type='uefa', goal_type='box', goal_alpha=.35, corner_arcs=True,
        pitch_color=bg_color, line_color=pitch_line, linewidth=1.15,
        pad_top=1.2, pad_bottom=1.2, pad_left=1.2, pad_right=1.2,
        line_zorder=3)
    p.draw(ax=ax)
    ax.set_facecolor(bg_color)
    return p


def _shade_left(ax):
    """Wash Opta left channel (high-y = top of a horizontal pitch)."""
    ax.fill_between([0, 105], LEFT_CORRIDOR_Y, 68, color=TEAL, alpha=0.07, zorder=0)


def _draw_zone14_halfspaces(ax):
    """Outlined Zone 14 + left half-space — no on-pitch words."""
    x0, x1, y0, y1 = ZONE14
    ax.add_patch(Rectangle(
        (x0, y0), x1 - x0, y1 - y0, fill=True, facecolor=COL_ZONE14,
        alpha=0.10, edgecolor=COL_ZONE14, lw=1.4, linestyle='--', zorder=1))
    lx0, lx1, ly0, ly1 = HALFSPACE_L
    ax.add_patch(Rectangle(
        (lx0, ly0), lx1 - lx0, ly1 - ly0, fill=True, facecolor=TEAL,
        alpha=0.08, edgecolor=TEAL, lw=1.2, linestyle='--', zorder=1))


def _pass_zone_counts(passes: pd.DataFrame) -> tuple[int, int]:
    """Count successful open-play ends in Zone 14 / left half-space."""
    if passes.empty:
        return 0, 0
    ok = passes
    if 'outcome' in ok.columns:
        ok = ok[ok.outcome == 'Successful']
    if ok.empty:
        return 0, 0
    z14 = int(((ok.end_x >= ZONE14[0]) & (ok.end_x <= ZONE14[1])
               & (ok.end_y >= ZONE14[2]) & (ok.end_y <= ZONE14[3])).sum())
    hs = int(((ok.end_x >= HALFSPACE_L[0])
              & (ok.end_y >= HALFSPACE_L[2]) & (ok.end_y <= HALFSPACE_L[3])).sum())
    return z14, hs


def _draw_zone_pass_arrows(ax, passes: pd.DataFrame, col: str):
    """Arrows for passes ending in Zone 14 (orange) or left half-space (teal)."""
    if passes.empty:
        return
    df = passes.dropna(subset=['x', 'y', 'end_x', 'end_y'])
    if 'outcome' in df.columns:
        df = df[df.outcome == 'Successful']
    for _, row in df.iterrows():
        x, y = float(row.x), float(row.y)
        ex, ey = float(row.end_x), float(row.end_y)
        in_z14 = (ZONE14[0] <= ex <= ZONE14[1] and ZONE14[2] <= ey <= ZONE14[3])
        in_lhs = (ex >= HALFSPACE_L[0] and HALFSPACE_L[2] <= ey <= HALFSPACE_L[3])
        if not (in_z14 or in_lhs):
            continue
        ax.add_patch(FancyArrowPatch(
            (x, y), (ex, ey), arrowstyle='->', mutation_scale=12,
            color=COL_ZONE14 if in_z14 else col, linewidth=1.5, alpha=0.85,
            zorder=4))


def _per90(value: float | None, minutes: float) -> float:
    if value is None or minutes <= 0:
        return 0.0
    return round(float(value) * 90.0 / minutes, 2)


def _metric_value(app: PlayerMatch, col: str, per90: bool) -> float:
    s = app.stats
    minutes = float(s.get('minutes_played') or 0)
    if col == 'pass_pct':
        att = float(s.get('passes') or 0)
        comp = float(s.get('passes_accurate') or 0)
        return round(100.0 * comp / att, 1) if att else 0.0
    if col == 'left_pct':
        return _left_share(app.events)
    if col == 'tackles_won':
        raw = (float(s.get('tackles_won') or 0)
               + float(s.get('interceptions') or 0)
               + float(s.get('ball_recoveries') or 0))
        return _per90(raw, minutes) if per90 else round(raw, 1)
    raw = float(s.get(col) or 0)
    return _per90(raw, minutes) if per90 else round(raw, 2)


def draw_footer(fig, y=0.018, size=9):
    fig.text(0.04, y, f'{AUTHOR}   ·   {AUTHOR_LINK}', color=MUTED,
             fontsize=size, ha='left', va='center')
    fig.text(0.96, y, CREDIT, color='#8A8A8A', fontsize=size, ha='right', va='center')


def _caption_bar(fig, spec, text: str):
    ax = fig.add_subplot(spec)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis('off')
    ax.set_facecolor(bg_color)
    ax.text(0.5, 0.55, text, ha='center', va='center', color=MUTED, fontsize=10)


def _logo(team='Arsenal'):
    path = ASSETS / 'arsenal.png'
    if path.exists():
        try:
            return mpimg.imread(path)
        except Exception:
            return None
    return None


def _fixture_bits(row: dict) -> tuple[str, str, str]:
    home, away = row['home'], row['away']
    opponent = away if home == 'Arsenal' else home
    venue = 'H' if home == 'Arsenal' else 'A'
    comp = row['competition'] or ''
    if 'Community Shield' in comp:
        return (f'Community Shield · Arsenal {row["score"]} {opponent}',
                'Community Shield', opponent)
    if row.get('matchweek'):
        return (f'PL MW{int(row["matchweek"])} · Arsenal {row["score"]} {opponent} ({venue})',
                f'PL MW{int(row["matchweek"])}', opponent)
    return (f'{comp} · Arsenal {row["score"]} {opponent}', comp or 'Match', opponent)


# --- data ------------------------------------------------------------------

def _events(cur, player_id: int, match_id: int) -> pd.DataFrame:
    cur.execute("""
        SELECT event_index, type, outcome, x, y, end_x, end_y,
               is_shot, is_goal, is_progressive, minute, second, team_id, player_id
          FROM events
         WHERE match_id = %s AND player_id = %s
         ORDER BY event_index
    """, (match_id, player_id))
    cols = [d[0] for d in cur.description]
    df = pd.DataFrame([dict(zip(cols, r)) for r in cur.fetchall()])
    for c in ('x', 'y', 'end_x', 'end_y'):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


def _stats(cur, player_id: int, match_id: int) -> dict | None:
    cur.execute("""
        SELECT shirt_no, position, is_first_eleven, minutes_played, rating,
               goals, assists, shots, shots_on_target, key_passes,
               touches, passes, passes_accurate, progressive_passes,
               crosses, xt, take_ons, take_ons_won,
               tackles, tackles_won, interceptions, ball_recoveries,
               clearances, blocks, aerials_won, dispossessed, fouls, fouled,
               avg_x, avg_y
          FROM match_player_stats
         WHERE player_id = %s AND match_id = %s
    """, (player_id, match_id))
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip([d[0] for d in cur.description], row))


def _links(cur, match_id: int, a: int, b: int) -> pd.DataFrame:
    cur.execute("""
        SELECT event_index, player_id, team_id, type, outcome,
               x, y, end_x, end_y, is_progressive, minute
          FROM events WHERE match_id = %s ORDER BY event_index
    """, (match_id,))
    g = pd.DataFrame([dict(zip([d[0] for d in cur.description], r))
                      for r in cur.fetchall()])
    if g.empty:
        return g
    for c in ('x', 'y', 'end_x', 'end_y', 'player_id', 'team_id'):
        g[c] = pd.to_numeric(g[c], errors='coerce')
    g['next_player'] = g['player_id'].shift(-1)
    g['next_team'] = g['team_id'].shift(-1)
    p = g[(g.type == 'Pass') & (g.outcome == 'Successful')].dropna(
        subset=['player_id', 'next_player', 'end_x', 'end_y'])
    p = p[p.team_id == p.next_team]
    return p[((p.player_id == a) & (p.next_player == b))
             | ((p.player_id == b) & (p.next_player == a))].reset_index(drop=True)


def load_bundle(player_id: int, partner_id: int) -> Bundle:
    with connect() as conn:
        cur = conn.cursor()

        def who(pid):
            cur.execute('SELECT player_id, name, short_name FROM players WHERE player_id=%s', (pid,))
            r = cur.fetchone()
            if not r:
                raise SystemExit(f'unknown player_id {pid}')
            return {'player_id': r[0], 'name': r[1], 'short_name': r[2]}

        player, partner = who(player_id), who(partner_id)

        cur.execute("""
            SELECT m.match_id, m.match_date::text, m.competition, m.matchweek,
                   th.name AS home, ta.name AS away,
                   m.home_score || '-' || m.away_score AS score
              FROM match_player_stats ps
              JOIN matches m ON m.match_id = ps.match_id
              JOIN teams th ON th.team_id = m.home_team_id
              JOIN teams ta ON ta.team_id = m.away_team_id
             WHERE ps.player_id = %s
             ORDER BY m.match_date
        """, (player_id,))
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        if len(rows) < 1:
            raise SystemExit(f'{player["name"]} has no appearances')

        player_apps, partner_apps, links = [], [], {}
        for row in rows:
            mid = int(row['match_id'])
            label, short, opp = _fixture_bits(row)
            ps = _stats(cur, player_id, mid) or {}
            player_apps.append(PlayerMatch(
                match_id=mid, date=row['match_date'], competition=row['competition'] or '',
                matchweek=row.get('matchweek'), opponent=opp, score=row['score'],
                short=short, label=label, stats=ps, events=_events(cur, player_id, mid),
                shirt=ps.get('shirt_no')))
            qs = _stats(cur, partner_id, mid)
            if qs:
                partner_apps.append(PlayerMatch(
                    match_id=mid, date=row['match_date'], competition=row['competition'] or '',
                    matchweek=row.get('matchweek'), opponent=opp, score=row['score'],
                    short=short, label=label, stats=qs, events=_events(cur, partner_id, mid),
                    shirt=qs.get('shirt_no')))
            links[mid] = _links(cur, mid, player_id, partner_id)

    return Bundle(player=player, partner=partner, player_apps=player_apps,
                  partner_apps=partner_apps, links=links)


# --- insight helpers -------------------------------------------------------

def _pass_df(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    return events[events.type == 'Pass'].dropna(subset=['x', 'y', 'end_x', 'end_y'])


def _left_share(events: pd.DataFrame) -> float:
    touches = events.dropna(subset=['y'])
    if touches.empty:
        return 0.0
    return round(100.0 * (touches.y >= LEFT_Y).mean(), 0)


def _insight_lines(b: Bundle) -> list[str]:
    """Concrete left-flank story for the dashboard caption panel."""
    lines = []
    total_ct = total_tc = 0
    for app in b.player_apps:
        lk = b.links.get(app.match_id, pd.DataFrame())
        if lk.empty:
            continue
        ct = int((lk.player_id == b.partner['player_id']).sum())
        tc = int((lk.player_id == b.player['player_id']).sum())
        total_ct += ct
        total_tc += tc
        tz_left = _left_share(app.events)
        pa = next((p for p in b.partner_apps if p.match_id == app.match_id), None)
        ca_left = _left_share(pa.events) if pa else 0
        lines.append(
            f"{app.short}: {ct + tc} combinations (Cala→Tzolis {ct}, Tzolis→Cala {tc}). "
            f"Tzolis {tz_left:.0f}% of touches on the left half; "
            f"Calafiori {ca_left:.0f}%."
        )

    # Role split
    a, c = b.player_apps[0], b.player_apps[-1] if len(b.player_apps) > 1 else b.player_apps[0]
    pa0 = next((p for p in b.partner_apps if p.match_id == a.match_id), None)
    pa1 = next((p for p in b.partner_apps if p.match_id == c.match_id), None)
    lines.append(
        f"Shield — Tzolis the creator (rating {a.stats.get('rating')}, "
        f"{a.stats.get('assists')} assists, {a.stats.get('key_passes')} key passes) "
        f"while Calafiori supplies from deeper "
        f"(avg x {pa0.stats.get('avg_x') if pa0 else '—'})."
    )
    if c.match_id != a.match_id and pa1:
        lines.append(
            f"PL MW1 — exchange flips two-way; Calafiori steps into midfield "
            f"(avg x {pa1.stats.get('avg_x')}, {pa1.stats.get('assists')} assist, "
            f"{pa1.stats.get('key_passes')} key pass) as Tzolis becomes the shooter "
            f"({c.stats.get('shots')} shots, avg x {c.stats.get('avg_x')})."
        )
    lines.append(
        f"Across both games: {total_ct + total_tc} successful links on the left flank "
        f"({total_ct} Cala→Tzolis / {total_tc} Tzolis→Cala) — Arsenal's progressive outlet."
    )
    return lines


# --- panel drawings (return caption; never write below the pitch) ----------

def _pass_length_m(passes: pd.DataFrame) -> float:
    if passes.empty:
        return 0.0
    dx = passes.end_x.astype(float) - passes.x.astype(float)
    dy = passes.end_y.astype(float) - passes.y.astype(float)
    return float(np.hypot(dx, dy).mean())


def panel_player_passes(ax, app: PlayerMatch, col: str, title_prefix: str) -> str:
    pitch = _pitch(ax)
    passes = _pass_df(app.events)
    n_all = 0 if passes.empty else len(passes)
    n_ok = int((passes.outcome == 'Successful').sum()) if n_all else 0
    n_prog = int((passes.is_progressive == True).sum()) if n_all else 0  # noqa: E712
    if n_all:
        ok = passes.outcome == 'Successful'
        prog = passes.is_progressive == True  # noqa: E712
        if (~ok).any():
            pitch.lines(passes.loc[~ok, 'x'], passes.loc[~ok, 'y'],
                        passes.loc[~ok, 'end_x'], passes.loc[~ok, 'end_y'],
                        ax=ax, color='#B0B0B0', lw=1.2, comet=True, zorder=2)
        if ok.any():
            pitch.lines(passes.loc[ok, 'x'], passes.loc[ok, 'y'],
                        passes.loc[ok, 'end_x'], passes.loc[ok, 'end_y'],
                        ax=ax, color=col, lw=1.8, comet=True, zorder=3)
        if prog.any():
            pitch.arrows(passes.loc[prog, 'x'], passes.loc[prog, 'y'],
                         passes.loc[prog, 'end_x'], passes.loc[prog, 'end_y'],
                         ax=ax, color=NAVY, width=1.4, headwidth=4, headlength=4,
                         zorder=4)

    shots = app.events[app.events.is_shot == True].dropna(subset=['x', 'y'])  # noqa: E712
    if not shots.empty:
        pitch.scatter(shots.x, shots.y, ax=ax, s=90, color=ARSENAL_RED,
                      edgecolors=line_color, lw=0.8, zorder=6)

    pct = (100.0 * n_ok / n_all) if n_all else 0.0
    avg_len = _pass_length_m(passes[passes.outcome == 'Successful']) if n_ok else 0.0
    s = app.stats
    ax.set_title(f'{title_prefix}  ·  {app.short}', color=INK, size=12,
                 fontweight='bold', pad=6)
    return (
        f"#{s.get('shirt_no')} {s.get('position')}  ·  {s.get('minutes_played')}′  ·  "
        f"rating {s.get('rating')}  ·  {n_ok}/{n_all} passes ({pct:.0f}%)  ·  "
        f"avg {avg_len:.0f} m  ·  {n_prog} progressive  ·  "
        f"{s.get('key_passes')} KP  ·  {s.get('assists')} A"
    )


def panel_player_actions(ax, app: PlayerMatch, col: str, title_prefix: str) -> str:
    pitch = _pitch(ax)
    types = {'Tackle', 'Interception', 'BallRecovery', 'BlockedPass', 'Clearance',
             'Aerial', 'Challenge', 'Foul'}
    acts = app.events[app.events.type.isin(types)].dropna(subset=['x', 'y'])
    if len(acts) >= 3:
        pitch.kdeplot(acts.x, acts.y, ax=ax, fill=True, levels=40, thresh=0.08,
                      cut=2, cmap=CMAP_HEAT, zorder=1)
    if not acts.empty:
        pitch.scatter(acts.x, acts.y, ax=ax, s=36, marker='o', color=NAVY,
                      alpha=0.7, zorder=3)

    avg_x, avg_y = app.stats.get('avg_x'), app.stats.get('avg_y')
    if avg_x is not None and avg_y is not None:
        pitch.scatter([float(avg_x)], [float(avg_y)], ax=ax, s=420, marker='o',
                      color=bg_color, edgecolors=line_color, lw=1.4, zorder=4)
        pitch.annotate(str(app.shirt or ''), xy=(float(avg_x), float(avg_y)),
                       c=col, ha='center', va='center', size=11, zorder=5, ax=ax)

    s = app.stats
    ax.set_title(f'{title_prefix} defensive actions  ·  {app.short}',
                 color=INK, size=12, fontweight='bold', pad=6)
    return (
        f"tackles won {s.get('tackles_won')}  ·  ints {s.get('interceptions')}  ·  "
        f"recoveries {s.get('ball_recoveries')}  ·  {len(acts)} actions plotted"
    )


def panel_partnership(ax, b: Bundle, match_id: int, short: str) -> str:
    pitch = _pitch(ax)
    _shade_left(ax)

    app = next(a for a in b.player_apps if a.match_id == match_id)
    partner = next((p for p in b.partner_apps if p.match_id == match_id), None)
    links = b.links.get(match_id, pd.DataFrame())

    if not links.empty:
        to_tz = links[links.next_player == b.player['player_id']]
        to_ca = links[links.next_player == b.partner['player_id']]
        if not to_tz.empty:
            pitch.arrows(to_tz.x, to_tz.y, to_tz.end_x, to_tz.end_y, ax=ax,
                         color=TEAL, width=1.5, headwidth=4, headlength=4,
                         alpha=0.85, zorder=3)
        if not to_ca.empty:
            pitch.arrows(to_ca.x, to_ca.y, to_ca.end_x, to_ca.end_y, ax=ax,
                         color=NAVY, width=1.5, headwidth=4, headlength=4,
                         alpha=0.85, zorder=3)

    nodes = []
    for stats, col, marker, label in (
        (app.stats, TEAL, 'o', f"#{app.shirt or 17}"),
        (partner.stats if partner else None, NAVY, 's',
         f"#{partner.shirt or 33}" if partner else ''),
    ):
        if not stats or stats.get('avg_x') is None:
            continue
        nodes.append((float(stats['avg_x']), float(stats['avg_y']), col, marker, label))

    if len(nodes) == 2 and not links.empty:
        n = len(links)
        color = (*to_rgba(TEAL)[:3], min(0.75, 0.22 + n * 0.05))
        pitch.lines([nodes[0][0]], [nodes[0][1]], [nodes[1][0]], [nodes[1][1]],
                    ax=ax, color=[color], lw=min(10, 2.5 + n), zorder=2)

    for x, y, col, marker, label in nodes:
        pitch.scatter([x], [y], ax=ax, s=520, marker=marker, color=bg_color,
                      edgecolors=line_color, lw=1.4, zorder=5)
        pitch.annotate(label, xy=(x, y), c=col, ha='center', va='center',
                       size=11, zorder=6, ax=ax)

    ct = int((links.player_id == b.partner['player_id']).sum()) if not links.empty else 0
    tc = int((links.player_id == b.player['player_id']).sum()) if not links.empty else 0
    ax.set_title(f'Left-flank combination  ·  {short}',
                 color=INK, size=12, fontweight='bold', pad=6)
    return (
        f'teal = Calafiori → Tzolis ({ct})   ·   navy = Tzolis → Calafiori ({tc})  ·  '
        f'nodes = average touch'
    )


def panel_left_corridor_heat(ax, b: Bundle, match_id: int, short: str) -> str:
    """Left-flank heat + Zone 14 / half-space ends. Counts live in the caption."""
    pitch = _pitch(ax)
    _shade_left(ax)
    _draw_zone14_halfspaces(ax)

    app = next(a for a in b.player_apps if a.match_id == match_id)
    partner = next((p for p in b.partner_apps if p.match_id == match_id), None)

    tz_all = app.events.dropna(subset=['x', 'y'])
    ca_all = (partner.events.dropna(subset=['x', 'y'])
              if partner is not None else pd.DataFrame())
    heat = pd.concat(
        [df[df.y >= LEFT_Y] for df in (tz_all, ca_all) if not df.empty],
        ignore_index=True) if (not tz_all.empty or not ca_all.empty) else tz_all

    if not heat.empty:
        bs = pitch.bin_statistic(heat.x, heat.y, statistic='count', bins=(12, 8))
        pitch.heatmap(bs, ax=ax, cmap=CMAP_HEAT, edgecolors=bg_color, alpha=0.88, zorder=1)
        pitch.scatter(heat.x, heat.y, ax=ax, s=14, color=NAVY, alpha=0.45, zorder=2)

    tz_passes = _pass_df(app.events)
    ca_passes = _pass_df(partner.events) if partner else pd.DataFrame()
    _draw_zone_pass_arrows(ax, tz_passes, TEAL)
    _draw_zone_pass_arrows(ax, ca_passes, NAVY)
    z14_t, hs_t = _pass_zone_counts(tz_passes)
    z14_c, hs_c = _pass_zone_counts(ca_passes)

    tz_left = _left_share(app.events)
    ca_left = _left_share(partner.events) if partner else 0
    ax.set_title(f'Left channel occupation  ·  {short}',
                 color=INK, size=12, fontweight='bold', pad=6)
    return (
        f'Zone 14 ends {z14_t + z14_c}  ·  L half-space ends {hs_t + hs_c}  ·  '
        f'Tzolis {tz_left:.0f}% left  ·  Calafiori {ca_left:.0f}% left  ·  '
        f'dashed boxes = Zone 14 (orange) / L half-space (teal)  ·  top = left flank'
    )


def _radar_values(app: PlayerMatch) -> list[float]:
    return [_metric_value(app, m[1], m[2]) for m in RADAR_METRICS]


def _fmt_radar(v: float, key: str) -> str:
    if key in ('pass_pct', 'left_pct'):
        return f'{v:.0f}'
    if key == 'rating':
        return f'{v:.2f}'
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f'{v:.1f}'


def panel_radar_compare(ax, app_a: PlayerMatch, app_b: PlayerMatch,
                        label_a: str, label_b: str, title: str,
                        col_a: str = TEAL, col_b: str = NAVY) -> str:
    """Radar with range ticks and vertex numbers."""
    ax.set_facecolor(bg_color)
    params = [m[0] for m in RADAR_METRICS]
    values_a = _radar_values(app_a)
    values_b = _radar_values(app_b)
    lows, highs = [], []
    for i, metric in enumerate(RADAR_METRICS):
        pair = [values_a[i], values_b[i]]
        lo, hi = 0.0, max(pair) * 1.25 if max(pair) > 0 else 1.0
        if metric[1] in ('pass_pct', 'left_pct'):
            lo, hi = 0.0, 100.0
        elif metric[1] == 'rating':
            lo, hi = 5.0, 10.0
        lows.append(lo)
        highs.append(hi)

    radar = Radar(params, lows, highs, num_rings=4, ring_width=1, center_circle_radius=1)
    radar.setup_axis(ax=ax, facecolor=bg_color)
    radar.draw_circles(ax=ax, facecolor='#EFEBE4', edgecolor='#C8C2B8')
    radar.draw_radar_compare(
        values_a, values_b, ax=ax,
        kwargs_radar={'facecolor': col_a, 'alpha': 0.38, 'edgecolor': col_a, 'lw': 2},
        kwargs_compare={'facecolor': col_b, 'alpha': 0.22, 'edgecolor': col_b, 'lw': 2},
    )
    radar.draw_param_labels(ax=ax, fontsize=9, color=INK)
    radar.draw_range_labels(ax=ax, fontsize=7, color=MUTED)
    # Vertex values: pair a/b next to each spoke.
    n = len(params)
    r_label = radar.center_circle_radius + radar.ring_width * (radar.num_rings + 0.55)
    for i, metric in enumerate(RADAR_METRICS):
        angle = (np.pi / 2) - (2 * np.pi * i / n)
        xa = r_label * np.cos(angle)
        ya = r_label * np.sin(angle)
        txt = (f'{_fmt_radar(values_a[i], metric[1])}/'
               f'{_fmt_radar(values_b[i], metric[1])}')
        ax.text(xa, ya, txt, ha='center', va='center', fontsize=7.5,
                color=INK, fontweight='bold', zorder=8,
                bbox=dict(boxstyle='round,pad=0.15', fc=bg_color, ec='none', alpha=0.85))
    ax.set_title(title, color=INK, size=12, fontweight='bold', pad=8)
    return (
        f'{label_a} (teal fill) / {label_b} (navy)  ·  '
        f'numbers at each spoke = {label_a} / {label_b}'
    )


def panel_radar_table(ax, app_a: PlayerMatch, app_b: PlayerMatch,
                      label_a: str, label_b: str):
    """Athletic-style Value columns beside the radar."""
    ax.set_facecolor(bg_color)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')
    ax.text(50, 94, 'Value', ha='center', color=INK, size=13, fontweight='bold')
    ax.plot([8, 92], [88, 88], color=pitch_line, lw=0.8)
    ax.text(8, 82, '', ha='left')
    ax.text(8, 82, 'Metric', ha='left', color=MUTED, size=9)
    ax.text(58, 82, label_a[:12], ha='center', color=TEAL, size=9, fontweight='bold')
    ax.text(84, 82, label_b[:12], ha='center', color=NAVY, size=9, fontweight='bold')
    va = _radar_values(app_a)
    vb = _radar_values(app_b)
    y = 72
    for i, metric in enumerate(RADAR_METRICS):
        ax.text(8, y, metric[0], ha='left', color=INK, size=10)
        ax.text(58, y, _fmt_radar(va[i], metric[1]), ha='center', color=INK,
                size=11, fontweight='bold')
        ax.text(84, y, _fmt_radar(vb[i], metric[1]), ha='center', color=INK,
                size=11, fontweight='bold')
        y -= 8.2


def panel_stats_table(ax, b: Bundle):
    """Match-report style scorecard — both players, both matches."""
    ax.set_facecolor(bg_color)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')
    ax.set_title('Player scorecard', color=INK, size=13, fontweight='bold', pad=6)

    headers = ['', 'Shield\nTzolis', 'Shield\nCala', 'PL MW1\nTzolis', 'PL MW1\nCala']
    rows_def = [
        ('Minutes', 'minutes_played'),
        ('Rating', 'rating'),
        ('Goals / Assists', None),
        ('Shots (SOT)', None),
        ('Key passes', 'key_passes'),
        ('Prog. passes', 'progressive_passes'),
        ('Touches', 'touches'),
        ('Pass acc.', None),
        ('Take-ons won', None),
        ('Tackles won', 'tackles_won'),
        ('Interceptions', 'interceptions'),
        ('Recoveries', 'ball_recoveries'),
        ('xT', 'xt'),
        ('Avg pos (x,y)', None),
        ('Left-touch %', None),
    ]

    def cell(app: PlayerMatch | None, label: str, key: str | None):
        if app is None:
            return '—'
        s = app.stats
        if label == 'Goals / Assists':
            return f"{s.get('goals')}/{s.get('assists')}"
        if label == 'Shots (SOT)':
            return f"{s.get('shots')} ({s.get('shots_on_target')})"
        if label == 'Pass acc.':
            a, c = s.get('passes') or 0, s.get('passes_accurate') or 0
            return f"{c}/{a}" if a else '—'
        if label == 'Take-ons won':
            return f"{s.get('take_ons_won')}/{s.get('take_ons')}"
        if label == 'Avg pos (x,y)':
            return f"{s.get('avg_x')}, {s.get('avg_y')}"
        if label == 'Left-touch %':
            return f"{_left_share(app.events):.0f}%"
        return str(s.get(key, '—'))

    tz = {a.match_id: a for a in b.player_apps}
    ca = {a.match_id: a for a in b.partner_apps}
    mids = [a.match_id for a in b.player_apps]
    m0, m1 = mids[0], mids[-1]

    # header row
    xs = [8, 28, 46, 66, 86]
    y = 90
    for x, h in zip(xs, headers):
        ax.text(x, y, h, ha='center', va='center', color=INK, size=9,
                fontweight='bold')
    ax.plot([2, 98], [84, 84], color=line_color, lw=1.5)

    y = 78
    for label, key in rows_def:
        vals = [
            label,
            cell(tz.get(m0), label, key),
            cell(ca.get(m0), label, key),
            cell(tz.get(m1), label, key),
            cell(ca.get(m1), label, key),
        ]
        for x, v in zip(xs, vals):
            ax.text(x, y, v, ha='center', va='center', color=INK, size=9,
                    fontweight='bold' if x == 8 else 'normal')
        y -= 5.2

    # link strip
    ax.plot([2, 98], [y + 2, y + 2], color='#CCCCCC', lw=1)
    y -= 2
    for mid, short in ((m0, b.player_apps[0].short), (m1, b.player_apps[-1].short)):
        lk = b.links.get(mid, pd.DataFrame())
        ct = int((lk.player_id == b.partner['player_id']).sum()) if not lk.empty else 0
        tc = int((lk.player_id == b.player['player_id']).sum()) if not lk.empty else 0
        ax.text(50, y, f'{short} links — Cala→Tzolis {ct}  ·  Tzolis→Cala {tc}',
                ha='center', color=TEAL_DARK, size=10, fontweight='bold')
        y -= 5


def panel_insights(ax, b: Bundle):
    ax.set_facecolor(bg_color)
    ax.axis('off')
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_title('How they run the left side', color=INK, size=13, fontweight='bold', pad=6)
    y = 84
    for line in _insight_lines(b):
        ax.text(5, y, '·  ' + line, ha='left', va='top', color=INK, size=9.5,
                wrap=True)
        y -= 18
    ax.plot([4, 96], [8, 8], color=pitch_line, lw=0.8)
    ax.text(50, 3, f'Left channel = UEFA y ≥ {LEFT_CORRIDOR_Y} (top of pitch)',
            ha='center', color=MUTED, size=9)


# --- page composers --------------------------------------------------------

def _mini_header(fig, spec, title: str, subtitle: str):
    ax = fig.add_subplot(spec)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')
    ax.set_facecolor(bg_color)
    logo = _logo()
    if logo is not None:
        inset = ax.inset_axes([0.88, 0.18, 0.11, 0.64])
        inset.imshow(logo)
        inset.axis('off')
    ax.text(4, 62, title, ha='left', va='center', color=INK,
            fontsize=22, fontweight='bold')
    ax.text(4, 28, subtitle, ha='left', va='center', color=MUTED, fontsize=11)
    ax.plot([0, 100], [4, 4], color=pitch_line, lw=0.9)


def _page(fig_size=(14.5, 9.2)):
    fig = plt.figure(figsize=fig_size, facecolor=bg_color)
    gs = GridSpec(3, 1, height_ratios=[0.85, 6.4, 0.55], hspace=0.08,
                  left=0.04, right=0.96, top=0.96, bottom=0.05, figure=fig)
    return fig, gs


def render_individual_pages(b: Bundle, out: Path, dpi: int = 160) -> list[Path]:
    written = []
    pairs = []
    for app in b.player_apps:
        pairs.append(('tzolis', app, TEAL, b.player['name']))
    for app in b.partner_apps:
        pairs.append(('calafiori', app, NAVY, b.partner['name']))

    for who, app, col, name in pairs:
        fig, gs = _page()
        _mini_header(fig, gs[0], f'{name}', f'{app.label}  ·  solo pass map')
        ax = fig.add_subplot(gs[1])
        cap = panel_player_passes(ax, app, col, name.split()[-1])
        _caption_bar(fig, gs[2], cap)
        draw_footer(fig)
        path = out / f"{len(written)+1:02d}_{who}_passes_{_slug(app.short)}.png"
        save_figure(fig, path, dpi=dpi)
        plt.close(fig)
        written.append(path)
        print(f'  wrote {path.name}')

        fig, gs = _page()
        _mini_header(fig, gs[0], f'{name}', f'{app.label}  ·  defensive actions')
        ax = fig.add_subplot(gs[1])
        cap = panel_player_actions(ax, app, col, name.split()[-1])
        _caption_bar(fig, gs[2], cap)
        draw_footer(fig)
        path = out / f"{len(written)+1:02d}_{who}_defensive_{_slug(app.short)}.png"
        save_figure(fig, path, dpi=dpi)
        plt.close(fig)
        written.append(path)
        print(f'  wrote {path.name}')

    for app in b.player_apps:
        fig, gs = _page()
        _mini_header(fig, gs[0],
                     f"{b.player['short_name']} × {b.partner['short_name']}",
                     f'{app.label}  ·  left-flank combination')
        ax = fig.add_subplot(gs[1])
        cap = panel_partnership(ax, b, app.match_id, app.short)
        _caption_bar(fig, gs[2], cap)
        draw_footer(fig)
        path = out / f"{len(written)+1:02d}_together_links_{_slug(app.short)}.png"
        save_figure(fig, path, dpi=dpi)
        plt.close(fig)
        written.append(path)
        print(f'  wrote {path.name}')

        fig, gs = _page()
        _mini_header(fig, gs[0],
                     f"{b.player['short_name']} × {b.partner['short_name']}",
                     f'{app.label}  ·  left channel occupation')
        ax = fig.add_subplot(gs[1])
        cap = panel_left_corridor_heat(ax, b, app.match_id, app.short)
        _caption_bar(fig, gs[2], cap)
        draw_footer(fig)
        path = out / f"{len(written)+1:02d}_together_corridor_{_slug(app.short)}.png"
        save_figure(fig, path, dpi=dpi)
        plt.close(fig)
        written.append(path)
        print(f'  wrote {path.name}')

    def _radar_page(title, subtitle, app_a, app_b, lab_a, lab_b, fname, col_a, col_b):
        fig = plt.figure(figsize=(15.2, 9.0), facecolor=bg_color)
        gs = GridSpec(3, 2, height_ratios=[0.8, 6.6, 0.5], width_ratios=[1.35, 0.65],
                      hspace=0.08, wspace=0.08, left=0.04, right=0.97, top=0.96,
                      bottom=0.05, figure=fig)
        _mini_header(fig, gs[0, :], title, subtitle)
        ax = fig.add_subplot(gs[1, 0])
        cap = panel_radar_compare(ax, app_a, app_b, lab_a, lab_b, title='',
                                  col_a=col_a, col_b=col_b)
        ax.set_title('')
        panel_radar_table(fig.add_subplot(gs[1, 1]), app_a, app_b, lab_a, lab_b)
        _caption_bar(fig, gs[2, :], cap)
        draw_footer(fig)
        path = out / fname
        save_figure(fig, path, dpi=dpi)
        plt.close(fig)
        written.append(path)
        print(f'  wrote {path.name}')

    if len(b.player_apps) >= 2:
        _radar_page(
            b.player['name'],
            f'{b.player_apps[0].short} vs {b.player_apps[-1].short}  ·  radar',
            b.player_apps[0], b.player_apps[-1],
            b.player_apps[0].short, b.player_apps[-1].short,
            f"{len(written)+1:02d}_tzolis_radar_shield_vs_pl.png",
            TEAL, NAVY)
    if len(b.partner_apps) >= 2:
        _radar_page(
            b.partner['name'],
            f'{b.partner_apps[0].short} vs {b.partner_apps[-1].short}  ·  radar',
            b.partner_apps[0], b.partner_apps[-1],
            b.partner_apps[0].short, b.partner_apps[-1].short,
            f"{len(written)+1:02d}_calafiori_radar_shield_vs_pl.png",
            TEAL, NAVY)
    for app in b.player_apps:
        partner = next((p for p in b.partner_apps if p.match_id == app.match_id), None)
        if not partner:
            continue
        _radar_page(
            f"{b.player['short_name']} vs {b.partner['short_name']}",
            f'{app.label}  ·  head-to-head radar',
            app, partner, b.player['short_name'], b.partner['short_name'],
            f"{len(written)+1:02d}_h2h_radar_{_slug(app.short)}.png",
            TEAL, NAVY)

    return written


def render_unified(b: Bundle, out: Path, dpi: int = 120) -> Path:
    """One collage — Athletic spacing, small panel titles only."""
    fig = plt.figure(figsize=(36, 48), facecolor=bg_color)
    gs = GridSpec(7, 3, height_ratios=[0.7, 1, 1, 1, 1, 1, 1.15],
                  hspace=0.22, wspace=0.10,
                  left=0.03, right=0.97, top=0.98, bottom=0.03, figure=fig)

    # header
    inner = GridSpecFromSubplotSpec(1, 3, subplot_spec=gs[0, :],
                                    width_ratios=[1, 5.2, 1], wspace=0.02)
    for i in (0, 2):
        ax = fig.add_subplot(inner[0, i])
        ax.axis('off')
        logo = _logo()
        if logo is not None:
            ax.imshow(logo)
    ax = fig.add_subplot(inner[0, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')
    ax.text(50, 72, f"{b.player['name']}  ×  {b.partner['name']}",
            color=INK, fontsize=28, fontweight='bold', ha='center', va='center')
    ax.text(50, 42, 'Left-flank analysis  ·  Community Shield & Premier League MW1',
            color=MUTED, fontsize=14, ha='center', va='center')
    bits = []
    for app in b.player_apps:
        lk = b.links.get(app.match_id, pd.DataFrame())
        n = len(lk)
        bits.append(f"{app.short}  {app.score} vs {app.opponent}  ({n} links)")
    ax.text(50, 18, '     ·     '.join(bits), color=MUTED, fontsize=12,
            ha='center', va='center')
    ax.plot([0, 100], [4, 4], color=pitch_line, lw=0.9)

    m0 = b.player_apps[0]
    m1 = b.player_apps[-1]
    p0 = next((p for p in b.partner_apps if p.match_id == m0.match_id), None)
    p1 = next((p for p in b.partner_apps if p.match_id == m1.match_id), None)

    # row 1 — solo passes Shield
    ax = fig.add_subplot(gs[1, 0])
    panel_player_passes(ax, m0, TEAL, 'Tzolis')
    ax = fig.add_subplot(gs[1, 1])
    panel_stats_table(ax, b)
    ax = fig.add_subplot(gs[1, 2])
    if p0:
        panel_player_passes(ax, p0, NAVY, 'Calafiori')
    else:
        ax.axis('off')

    # row 2 — solo passes PL + insights
    ax = fig.add_subplot(gs[2, 0])
    panel_player_passes(ax, m1, TEAL, 'Tzolis')
    ax = fig.add_subplot(gs[2, 1])
    panel_insights(ax, b)
    ax = fig.add_subplot(gs[2, 2])
    if p1:
        panel_player_passes(ax, p1, NAVY, 'Calafiori')
    else:
        ax.axis('off')

    # row 3 — together links + corridor PL
    ax = fig.add_subplot(gs[3, 0])
    panel_partnership(ax, b, m0.match_id, m0.short)
    ax = fig.add_subplot(gs[3, 1])
    panel_left_corridor_heat(ax, b, m1.match_id, m1.short)
    ax = fig.add_subplot(gs[3, 2])
    panel_partnership(ax, b, m1.match_id, m1.short)

    # row 4 — corridor Shield + defensive
    ax = fig.add_subplot(gs[4, 0])
    panel_player_actions(ax, m0, TEAL, 'Tzolis')
    ax = fig.add_subplot(gs[4, 1])
    panel_left_corridor_heat(ax, b, m0.match_id, m0.short)
    ax = fig.add_subplot(gs[4, 2])
    if p0:
        panel_player_actions(ax, p0, NAVY, 'Calafiori')
    else:
        ax.axis('off')

    # row 5 — PL defensive + takeaway
    ax = fig.add_subplot(gs[5, 0])
    panel_player_actions(ax, m1, TEAL, 'Tzolis')
    ax = fig.add_subplot(gs[5, 1])
    ax.set_facecolor(bg_color)
    ax.axis('off')
    ax.set_title('Key takeaway', color=INK, size=13, fontweight='bold', pad=6)
    take = (
        "Calafiori builds; Tzolis finishes the left lane.\n\n"
        "Shield = vertical supply and Tzolis as creator.\n"
        "PL MW1 = two-way link + Tzolis shot volume.\n\n"
        "Together they are Arsenal's left progressive corridor."
    )
    ax.text(0.5, 0.55, take, transform=ax.transAxes, ha='center', va='center',
            color=INK, size=12, linespacing=1.45)
    ax = fig.add_subplot(gs[5, 2])
    if p1:
        panel_player_actions(ax, p1, NAVY, 'Calafiori')
    else:
        ax.axis('off')

    # row 6 — radars (both players across matches + H2H PL)
    ax = fig.add_subplot(gs[6, 0])
    panel_radar_compare(
        ax, m0, m1, m0.short, m1.short,
        f"{b.player['short_name']} — Shield vs PL",
        col_a=TEAL, col_b=NAVY)
    ax = fig.add_subplot(gs[6, 1])
    if p0 and p1:
        # H2H for PL as the more recent league sample; Shield H2H is in individuals
        panel_radar_compare(
            ax, m1, p1, b.player['short_name'], b.partner['short_name'],
            f'H2H radar  ·  {m1.short}',
            col_a=TEAL, col_b=NAVY)
    else:
        ax.axis('off')
    ax = fig.add_subplot(gs[6, 2])
    if p0 and p1:
        panel_radar_compare(
            ax, p0, p1, p0.short, p1.short,
            f"{b.partner['short_name']} — Shield vs PL",
            col_a=TEAL, col_b=NAVY)
    else:
        ax.axis('off')

    draw_footer(fig, y=0.015, size=16)
    path = out / '00_dashboard_unified.png'
    save_figure(fig, path, dpi=dpi)
    plt.close(fig)
    print(f'  wrote {path.name}')
    return path


def write_report(b: Bundle, out: Path, paths: list[Path]) -> Path:
    lines = [
        f"# {b.player['name']} × {b.partner['name']} — left-flank analysis",
        '',
        f'{AUTHOR}',
        '',
        'Visual language follows the Athletic Ødegaard board (cream pitch, '
        'teal passes, red heat, captions below the grass, numbered radars).',
        '',
        '## Insights',
        '',
    ]
    for line in _insight_lines(b):
        lines.append(f'- {line}')
    lines += ['', '## Panels', '']
    for p in paths:
        lines.append(f'- `{p.name}`')
    lines += ['', f'_{CREDIT}_', '']
    path = out / 'REPORT.md'
    path.write_text('\n'.join(lines))
    return path


def render(player_id: int = TZOLIS_ID, partner_id: int = CALAFIORI_ID,
           out_root: Path | None = None) -> Path:
    b = load_bundle(player_id, partner_id)
    out = out_root or (ROOT / 'output' / 'players_performance' /
                       f"{_slug(b.player['name'])}_{_slug(b.partner['name'])}_left_flank")
    out.mkdir(parents=True, exist_ok=True)

    # clear old experimental files in the legacy folder pointer
    print(f'rendering → {out}')
    paths: list[Path] = []
    paths.append(render_unified(b, out, dpi=130))
    paths.extend(render_individual_pages(b, out, dpi=170))
    report = write_report(b, out, paths)
    (out / 'manifest.json').write_text(json.dumps({
        'handle': AUTHOR,
        'player': b.player,
        'partner': b.partner,
        'charts': [p.name for p in paths],
        'report': report.name,
    }, indent=2))
    print(f'done — {len(paths)} charts + {report.name}')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--player-id', type=int, default=TZOLIS_ID)
    ap.add_argument('--partner-id', type=int, default=CALAFIORI_ID)
    ap.add_argument('--out', type=Path, default=None)
    args = ap.parse_args()
    render(args.player_id, args.partner_id, args.out)


if __name__ == '__main__':
    main()
