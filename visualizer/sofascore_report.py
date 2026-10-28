#!/usr/bin/env python3
"""Post-match report rendered from a SofaScore bundle.

Used when WhoScored has no Opta event stream for a match (friendlies, lower
tiers). SofaScore exposes rich aggregates but **no pass-by-pass event feed**, so
this renders the subset of panels that its data genuinely supports - nine of
them - rather than faking the rest. See README / docs/VISUALS.md for the split.

Coordinate systems (verified empirically against event 16404394):
  * heatmap / average-positions  x: 0 = own goal, 100 = opponent goal; y: 0-100 lateral
  * shotmap playerCoordinates    x: distance from the goal being attacked; y: 0-100

    python visualizer/sofascore_report.py --bundle <bundle.json> --out final_output_sofascore
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use('Agg')

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.gridspec import GridSpec
from mplsoccer import Pitch

from visualizer.match_report import AUTHOR, AUTHOR_LINK, bg_color, line_color, path_eff

CREDIT = 'Data: SofaScore  |  Built with mplsoccer'
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PITCH_L, PITCH_W = 105.0, 68.0

M = SimpleNamespace()


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------

def prepare(bundle_path: str) -> SimpleNamespace:
    with open(bundle_path) as fh:
        b = json.load(fh)

    event = b['event']
    home, away = event['homeTeam'], event['awayTeam']

    M.__dict__.update(dict(
        bundle=b,
        event_id=b['event_id'],
        hteamName=home['name'], ateamName=away['name'],
        hteamId=home['id'], ateamId=away['id'],
        hgoal=event['homeScore']['current'], agoal=event['awayScore']['current'],
        ht_home=event['homeScore'].get('period1'), ht_away=event['awayScore'].get('period1'),
        competition=(event.get('tournament') or {}).get('name', ''),
        venue=((event.get('venue') or {}).get('stadium') or {}).get('name', ''),
        date=_date(event.get('startTimestamp')),
        stats=_team_stats(b),
        shots=_shots(b),
        positions=_positions(b),
        heat=_heatmaps(b),
        players=_players(b),
        momentum=_momentum(b),
    ))
    M.hcol, M.acol = _resolve_colors(home, away)
    return M


# Fallbacks that read clearly on white and are far apart from each other.
PALETTE = ['#1B458F', '#0E7C66', '#6A1B9A', '#B25E00', '#00658F', '#8B1A3B']


def _resolve_colors(home: dict, away: dict) -> tuple[str, str]:
    """Pick two readable, visually distinct team colours.

    SofaScore reports a club's actual kit colour, which is often white (Girona,
    Real Madrid, Spurs) - invisible on a white background - and two red clubs
    would render identically. So: prefer the curated map, reject anything too
    light, then force the pair apart.
    """
    from visualizer.palette import clash, resolve, team_color

    def candidate(team: dict) -> str | None:
        curated = team_color(team['name'])
        if curated:
            return curated
        colors = team.get('teamColors') or {}
        for key in ('primary', 'secondary'):
            value = colors.get(key)
            if value and _luminance(value) < 0.75:
                return value
        return None

    hcol = candidate(home)
    acol = candidate(away)

    used = [c for c in (hcol, acol) if c]
    if hcol is None:
        hcol = _pick_distinct(used)
        used.append(hcol)
    if acol is None:
        acol = _pick_distinct(used)

    # Both curated: the shared resolver knows each club's change strip.
    if team_color(home['name']) and team_color(away['name']):
        return resolve(home['name'], away['name'])

    if clash(hcol, acol):
        # Keep whichever is curated (the club we actually care about) and move
        # the other one to a contrasting palette entry.
        if team_color(away['name']):
            hcol = _pick_distinct([acol])
        else:
            acol = _pick_distinct([hcol])
    return hcol, acol


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(hex_color: str) -> float:
    r, g, b = _rgb(hex_color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _distance(a: str, b: str) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5


def _pick_distinct(used: list[str]) -> str:
    best, best_gap = PALETTE[0], -1.0
    for option in PALETTE:
        gap = min((_distance(option, u) for u in used), default=99)
        if gap > best_gap:
            best, best_gap = option, gap
    return best


def _date(ts):
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d') if ts else ''


def _team_stats(b) -> dict:
    """Flatten the ALL-period statistics groups into {name: (home, away)}."""
    out = {}
    blocks = ((b.get('statistics') or {}).get('statistics') or [])
    for period in blocks:
        if period.get('period') != 'ALL':
            continue
        for group in period.get('groups', []):
            for item in group.get('statisticsItems', []):
                out[item['name']] = (item.get('home'), item.get('away'))
    return out


def _shots(b) -> list:
    out = []
    for s in ((b.get('shotmap') or {}).get('shotmap') or []):
        pc = s.get('playerCoordinates') or {}
        if 'x' not in pc:
            continue
        out.append({
            'is_home': bool(s['isHome']),
            'minute': s.get('time'),
            'type': s.get('shotType'),          # goal|save|miss|block|post
            'situation': s.get('situation'),
            'body_part': s.get('bodyPart'),
            'player': s['player'].get('shortName'),
            # x is distance from the attacked goal -> flip into an attacking frame
            'x': (100.0 - float(pc['x'])) / 100.0 * PITCH_L,
            'y': float(pc['y']) / 100.0 * PITCH_W,
        })
    return out


def _positions(b) -> dict:
    out = {'home': [], 'away': []}
    ap = b.get('average_positions') or {}
    for side in ('home', 'away'):
        for e in ap.get(side) or []:
            if e.get('averageX') is None:
                continue
            out[side].append({
                'name': e['player'].get('shortName'),
                'shirt': e['player'].get('jerseyNumber'),
                'position': e['player'].get('position'),
                'touches': e.get('pointsCount') or 0,
                'x': float(e['averageX']) / 100.0 * PITCH_L,
                'y': float(e['averageY']) / 100.0 * PITCH_W,
            })
    return out


def _heatmaps(b) -> dict:
    """Aggregate every player's touch points into one cloud per team."""
    lineups, heat = b.get('lineups') or {}, b.get('heatmaps') or {}
    out = {'home': [], 'away': []}
    for side in ('home', 'away'):
        for entry in (lineups.get(side) or {}).get('players', []):
            pid = str(entry.get('player', {}).get('id'))
            for pt in heat.get(pid, []):
                out[side].append((float(pt['x']) / 100.0 * PITCH_L,
                                  float(pt['y']) / 100.0 * PITCH_W))
    return out


def _players(b) -> dict:
    out = {'home': [], 'away': []}
    lineups = b.get('lineups') or {}
    for side in ('home', 'away'):
        for e in (lineups.get(side) or {}).get('players', []):
            st = e.get('statistics') or {}
            if not st:
                continue
            out[side].append({
                'name': e['player'].get('shortName'),
                'position': e.get('position'),
                'rating': st.get('rating'),
                'minutes': st.get('minutesPlayed', 0),
                'touches': st.get('touches', 0),
                'passes': st.get('totalPass', 0),
                'accurate': st.get('accuratePass', 0),
                'key_passes': st.get('keyPass', 0),
                'goals': st.get('goals', 0),
                'assists': st.get('goalAssist', 0),
                'shots': st.get('totalShots', 0),
                'tackles': st.get('totalTackle', 0),
                'duels_won': st.get('duelWon', 0),
            })
    return out


def _momentum(b) -> list:
    return [(p.get('minute'), p.get('value')) for p in ((b.get('graph') or {}).get('graphPoints') or [])]


# ---------------------------------------------------------------------------
# Crests (SofaScore serves one for every team, so this works for any fixture)
# ---------------------------------------------------------------------------

def team_logo(team_id: int, team_name: str):
    from visualizer.match_report import load_logo
    local = load_logo(team_name)
    if local is not None:
        return local

    path = os.path.join(ASSETS, f'sofascore_team_{team_id}.png')
    if not os.path.exists(path):
        try:
            import requests
            r = requests.get(f'https://api.sofascore.com/api/v1/team/{team_id}/image',
                             headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            r.raise_for_status()
            with open(path, 'wb') as fh:
                fh.write(r.content)
        except Exception:                                        # noqa: BLE001
            return None
    try:
        return mpimg.imread(path)
    except Exception:                                            # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def panel_shotmap(ax):
    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.5, corner_arcs=True,
                  pitch_color=bg_color, linewidth=2, line_color=line_color)
    pitch.draw(ax=ax)

    style = {'goal': ('football', 380, 'green'), 'save': ('o', 220, None),
             'post': ('o', 220, 'orange'), 'miss': ('o', 220, None), 'block': ('X', 180, 'gray')}

    for s in M.shots:
        col = M.hcol if s['is_home'] else M.acol
        # mirror the home team so both attack the middle
        x, y = (PITCH_L - s['x'], PITCH_W - s['y']) if s['is_home'] else (s['x'], s['y'])
        marker, size, edge = style.get(s['type'], ('o', 200, None))
        if s['type'] == 'goal':
            pitch.scatter(x, y, s=size, marker='football', c='None',
                          edgecolors='green', linewidths=0.8, zorder=4, ax=ax)
        elif s['type'] == 'save':
            pitch.scatter(x, y, s=size, marker='o', c='None', edgecolors=col,
                          hatch='///////', zorder=3, ax=ax)
        elif s['type'] == 'post':
            pitch.scatter(x, y, s=size, marker='o', c=col, edgecolors='orange',
                          linewidths=2, zorder=3, ax=ax)
        elif s['type'] == 'block':
            pitch.scatter(x, y, s=size, marker='X', c='None', edgecolors=col, zorder=2, ax=ax)
        else:
            pitch.scatter(x, y, s=size, marker='o', c='None', edgecolors=col, zorder=2, ax=ax)

    h = [s for s in M.shots if s['is_home']]
    a = [s for s in M.shots if not s['is_home']]
    ax.text(0, 70, f'{M.hteamName} shots', color=M.hcol, size=22, ha='left',
            fontweight='bold', path_effects=path_eff)
    ax.text(105, 70, f'{M.ateamName} shots', color=M.acol, size=22, ha='right',
            fontweight='bold', path_effects=path_eff)
    ax.text(52.5, -4, f'{len(h)} v {len(a)} shots   |   ball = goal, hatched = saved, '
                      'hollow = off target, X = blocked, filled = woodwork',
            color='gray', size=12, ha='center', va='center')
    ax.set_title('Shot Map', color=line_color, fontsize=26, fontweight='bold', path_effects=path_eff)


def panel_shape(ax, side):
    players = M.positions[side]
    col = M.hcol if side == 'home' else M.acol
    name = M.hteamName if side == 'home' else M.ateamName

    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.5, corner_arcs=True,
                  pitch_color=bg_color, line_color=line_color, linewidth=2)
    pitch.draw(ax=ax)

    if not players:
        ax.text(52.5, 34, 'no average positions', ha='center', va='center', color='gray', size=18)
        return

    top = max(p['touches'] for p in players) or 1
    for p in players:
        size = 400 + p['touches'] / top * 2200
        pitch.scatter(p['x'], p['y'], s=size, marker='o', color=bg_color,
                      edgecolor=col, linewidth=2.5, alpha=1, zorder=3, ax=ax)
        pitch.annotate(p['shirt'], xy=(p['x'], p['y']), c=col, ha='center', va='center',
                       size=15, fontweight='bold', zorder=4, ax=ax)

    avg = np.mean([p['x'] for p in players])
    ax.axvline(avg, color='gray', linestyle='--', alpha=0.75, linewidth=2)
    if side == 'away':
        ax.invert_xaxis()
        ax.invert_yaxis()
        ax.text(0, 73, '<--- Attacking Direction', color=col, size=14, ha='right', va='center')
    else:
        ax.text(0, -5, 'Attacking Direction --->', color=col, size=14, ha='left', va='center')

    ax.set_title(f'{name} Average Positions', color=line_color, fontsize=26,
                 fontweight='bold', path_effects=path_eff)
    ax.text(52.5, 70, f'node size = touches   |   avg. line height {avg:.1f}m',
            color='gray', size=12, ha='center', va='center')


def panel_territory(ax, side):
    pts = M.heat[side]
    col = M.hcol if side == 'home' else M.acol
    name = M.hteamName if side == 'home' else M.ateamName

    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.5, corner_arcs=True,
                  pitch_color=bg_color, line_color=line_color, linewidth=2, line_zorder=3)
    pitch.draw(ax=ax)

    if len(pts) < 10:
        ax.text(52.5, 34, 'no heatmap data', ha='center', va='center', color='gray', size=18)
        return

    xs, ys = zip(*pts)
    cmap = LinearSegmentedColormap.from_list('t', [bg_color, col], N=500)
    pitch.kdeplot(np.array(xs), np.array(ys), ax=ax, fill=True, levels=400,
                  thresh=0.02, cut=4, cmap=cmap, zorder=1)

    final_third = sum(1 for x in xs if x >= 70) / len(xs) * 100
    if side == 'away':
        ax.invert_xaxis()
        ax.invert_yaxis()
        ax.text(0, 73, '<--- Attacking Direction', color=col, size=14, ha='right', va='center')
    else:
        ax.text(0, -5, 'Attacking Direction --->', color=col, size=14, ha='left', va='center')

    ax.set_title(f'{name} Territory', color=line_color, fontsize=26,
                 fontweight='bold', path_effects=path_eff)
    ax.text(52.5, 70, f'{len(pts)} touches   |   {final_third:.0f}% in the final third',
            color='gray', size=12, ha='center', va='center')


STAT_ROWS = [
    ('Ball possession', 'Possession'),
    ('Expected goals', 'xG'),
    ('Big chances', 'Big chances'),
    ('Total shots', 'Shots'),
    ('Shots on target', 'On target'),
    ('Accurate passes', 'Accurate passes'),
    ('Final third entries', 'Final third entries'),
    ('Corner kicks', 'Corners'),
    ('Total tackles', 'Tackles'),
    ('Interceptions', 'Interceptions'),
    ('Recoveries', 'Recoveries'),
    ('Duels', 'Duels won'),
]


def _stat_row_usable(key: str) -> bool:
    """Drop Expected goals when SofaScore only has placeholder 0–0."""
    if key not in M.stats:
        return False
    if key != 'Expected goals':
        return True
    hv, av = M.stats[key]
    try:
        return not (float(hv) == 0.0 and float(av) == 0.0)
    except (TypeError, ValueError):
        return False


def panel_match_stats(ax):
    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.75, corner_arcs=True,
                  pitch_color=bg_color, line_color=bg_color, linewidth=2)
    pitch.draw(ax=ax)

    ax.fill([0, 0, 105, 105], [62, 68, 68, 62], 'orange')
    ax.text(52.5, 64.5, 'Match Stats', ha='center', va='center', color=line_color,
            fontsize=24, fontweight='bold', path_effects=path_eff)

    rows = [row for row in STAT_ROWS if _stat_row_usable(row[0])][:12]
    ys = [58 - i * 5 for i in range(len(rows))]

    for y, (key, label) in zip(ys, rows):
        hv, av = M.stats[key]
        hn, an = _num(hv), _num(av)
        total = hn + an
        hw = (hn / total * 45) if total else 22.5
        aw = (an / total * 45) if total else 22.5
        ax.barh([y], [-hw], height=3.4, color=M.hcol, left=52.5)
        ax.barh([y], [aw], height=3.4, color=M.acol, left=52.5)
        ax.text(52.5, y, label, color=line_color, fontsize=14, ha='center', va='center',
                fontweight='bold', path_effects=path_eff)
        ax.text(6, y, str(hv), color=line_color, fontsize=16, ha='right', va='center', fontweight='bold')
        ax.text(99, y, str(av), color=line_color, fontsize=16, ha='left', va='center', fontweight='bold')

    ax.text(6, 66, M.hteamName, color=M.hcol, fontsize=18, ha='right', va='center', fontweight='bold')
    ax.text(99, 66, M.ateamName, color=M.acol, fontsize=18, ha='left', va='center', fontweight='bold')
    for s in ('top', 'right', 'bottom', 'left'):
        ax.spines[s].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])


def _num(v):
    """'79/95 (83%)' -> 79 ; '47%' -> 47 ; '1.00' -> 1.0"""
    if v is None:
        return 0.0
    s = str(v).split('(')[0].strip().rstrip('%')
    if '/' in s:
        s = s.split('/')[0]
    try:
        return float(s)
    except ValueError:
        return 0.0


def panel_momentum(ax):
    if not M.momentum:
        ax.text(0.5, 0.5, 'no momentum data', ha='center', va='center', transform=ax.transAxes)
        ax.axis('off')
        return
    minutes = [m for m, _ in M.momentum]
    values = [v for _, v in M.momentum]
    colors = [M.hcol if v > 0 else M.acol for v in values]

    ax.bar(minutes, values, color=colors, width=1.0)
    ax.axhline(0, color=line_color, linewidth=2)
    ax.axvline(45, color='gray', linewidth=2, linestyle='dashed')
    for s in ('top', 'right', 'left', 'bottom'):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis='both', length=0, labelsize=12)
    ax.set_xlabel('Minute', color=line_color, fontsize=16)
    ax.set_ylabel('Attacking pressure', color=line_color, fontsize=16)
    ax.set_facecolor(bg_color)

    lim = max(abs(min(values)), abs(max(values))) or 1
    ax.set_ylim(-lim * 1.35, lim * 1.35)
    ax.text(1, lim * 1.2, M.hteamName, color=M.hcol, fontsize=15, fontweight='bold', va='center')
    ax.text(1, -lim * 1.2, M.ateamName, color=M.acol, fontsize=15, fontweight='bold', va='center')
    ax.set_title('Match Momentum', color=line_color, fontsize=26, fontweight='bold')


def panel_top_performers(ax):
    rows = []
    for side in ('home', 'away'):
        for p in M.players[side]:
            if p['rating'] and p['minutes'] >= 20:
                rows.append((p['rating'], p['name'], side, p))
    rows.sort(reverse=True, key=lambda r: r[0])
    rows = rows[:12][::-1]

    if not rows:
        ax.text(0.5, 0.5, 'no player ratings', ha='center', va='center', transform=ax.transAxes)
        ax.axis('off')
        return

    names = [f"{r[1]}" for r in rows]
    vals = [r[0] for r in rows]
    cols = [M.hcol if r[2] == 'home' else M.acol for r in rows]

    ax.barh(range(len(rows)), vals, color=cols, height=0.72)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(names, fontsize=13)
    ax.set_xlim(min(vals) - 0.6, max(vals) + 0.35)
    for i, (v, r) in enumerate(zip(vals, rows)):
        extra = []
        if r[3]['goals']:
            extra.append(f"{r[3]['goals']}G")
        if r[3]['assists']:
            extra.append(f"{r[3]['assists']}A")
        label = f"{v:.1f}" + (f"  ({', '.join(extra)})" if extra else '')
        ax.text(v + 0.03, i, label, va='center', fontsize=12, color=line_color, fontweight='bold')
    for s in ('top', 'right', 'bottom'):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis='x', length=0, labelbottom=False)
    ax.tick_params(axis='y', length=0)
    ax.set_facecolor(bg_color)
    ax.set_title('Top Performers by Rating', color=line_color, fontsize=26, fontweight='bold')


def panel_shot_breakdown(ax):
    ax.set_facecolor(bg_color)
    ax.axis('off')
    ax.set_title('Shot Profile', color=line_color, fontsize=26, fontweight='bold')

    def tally(key):
        out = {}
        for s in M.shots:
            k = (s.get(key) or 'unknown').replace('-', ' ')
            side = 'h' if s['is_home'] else 'a'
            out.setdefault(k, {'h': 0, 'a': 0})[side] += 1
        return out

    y = 0.92
    for title, key in (('By situation', 'situation'), ('By body part', 'body_part'),
                       ('By outcome', 'type')):
        ax.text(0.5, y, title, ha='center', va='center', fontsize=17, fontweight='bold',
                transform=ax.transAxes, color=line_color)
        y -= 0.07
        data = sorted(tally(key).items(), key=lambda kv: -(kv[1]['h'] + kv[1]['a']))
        for label, counts in data[:5]:
            total = counts['h'] + counts['a']
            ax.text(0.44, y, label, ha='right', va='center', fontsize=14,
                    transform=ax.transAxes, color=line_color)
            width = 0.30 * total / max(1, len(M.shots))
            hw = width * counts['h'] / total if total else 0
            ax.barh([y], [hw], left=0.48, height=0.035, color=M.hcol, transform=ax.transAxes)
            ax.barh([y], [width - hw], left=0.48 + hw, height=0.035, color=M.acol,
                    transform=ax.transAxes)
            ax.text(0.80, y, f"{counts['h']} - {counts['a']}", ha='left', va='center',
                    fontsize=13, transform=ax.transAxes, color=line_color, fontweight='bold')
            y -= 0.055
        y -= 0.04


# ---------------------------------------------------------------------------
# Header / footer / layout
# ---------------------------------------------------------------------------

def draw_header(fig, spec, big=True):
    from matplotlib.gridspec import GridSpecFromSubplotSpec
    inner = GridSpecFromSubplotSpec(1, 3, subplot_spec=spec, width_ratios=[1, 5.2, 1], wspace=0.02)

    for i, (tid, tname) in ((0, (M.hteamId, M.hteamName)), (2, (M.ateamId, M.ateamName))):
        ax = fig.add_subplot(inner[0, i])
        ax.axis('off')
        logo = team_logo(tid, tname)
        if logo is not None:
            ax.imshow(logo)

    ax = fig.add_subplot(inner[0, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')
    sc = 1.0 if big else 0.62

    ax.text(50, 78, f'{M.hteamName}  {M.hgoal} - {M.agoal}  {M.ateamName}',
            color=line_color, fontsize=44 * sc, fontweight='bold', ha='center', va='center')
    sub = ' | '.join(x for x in (M.competition, M.date, M.venue) if x)
    if M.ht_home is not None:
        sub += f' | HT {M.ht_home}:{M.ht_away}'
    ax.text(50, 47, sub, color='#444444', fontsize=21 * sc, ha='center', va='center')

    xg = M.stats.get('Expected goals')
    poss = M.stats.get('Ball possession')
    bits = []
    if xg:
        try:
            if not (float(xg[0]) == 0.0 and float(xg[1]) == 0.0):
                bits.append(f'xG  {xg[0]} - {xg[1]}')
        except (TypeError, ValueError, IndexError):
            bits.append(f'xG  {xg[0]} - {xg[1]}')
    if poss:
        bits.append(f'Possession  {poss[0]} - {poss[1]}')
    bits.append(f'Shots  {sum(1 for s in M.shots if s["is_home"])} - '
                f'{sum(1 for s in M.shots if not s["is_home"])}')
    ax.text(50, 22, '     '.join(bits), color='#444444', fontsize=19 * sc, ha='center', va='center')

    ax.plot([0, 100], [2, 2], color=line_color, linewidth=2)
    ax.plot([0, 100], [98, 98], color=line_color, linewidth=2)


def draw_footer(fig, y=0.012, size=11):
    try:
        from highlight_text import fig_text
        fig_text(0.012, y, f'<{AUTHOR}>   ·   {AUTHOR_LINK}', fig=fig, color='#777777',
                 fontsize=size, ha='left', va='center',
                 highlight_textprops=[{'color': line_color, 'fontweight': 'bold',
                                       'fontsize': size * 1.25}])
    except Exception:                                            # noqa: BLE001
        fig.text(0.012, y, f'{AUTHOR}   ·   {AUTHOR_LINK}', color=line_color,
                 fontsize=size, ha='left', va='center')
    fig.text(0.988, y, CREDIT, color='#999999', fontsize=size, ha='right', va='center')


PANELS = [
    ('01_shot_map', panel_shotmap),
    ('02_match_stats', panel_match_stats),
    ('03_momentum', panel_momentum),
    ('04_home_average_positions', lambda ax: panel_shape(ax, 'home')),
    ('05_top_performers', panel_top_performers),
    ('06_away_average_positions', lambda ax: panel_shape(ax, 'away')),
    ('07_home_territory', lambda ax: panel_territory(ax, 'home')),
    ('08_shot_profile', panel_shot_breakdown),
    ('09_away_territory', lambda ax: panel_territory(ax, 'away')),
]


def render_individual(out_dir, dpi=150):
    written = []
    for name, fn in PANELS:
        fig = plt.figure(figsize=(14, 12), facecolor=bg_color)
        gs = GridSpec(2, 1, height_ratios=[1, 5.4], hspace=0.20, figure=fig)
        draw_header(fig, gs[0], big=False)
        ax = fig.add_subplot(gs[1])
        ax.set_facecolor(bg_color)
        fn(ax)
        draw_footer(fig)
        path = os.path.join(out_dir, f'{name}.png')
        fig.savefig(path, bbox_inches='tight', facecolor=bg_color, dpi=dpi)
        plt.close(fig)
        written.append(path)
        print(f'  wrote {os.path.basename(path)}')
    return written


def render_dashboard(out_dir, dpi=100):
    fig = plt.figure(figsize=(40, 34), facecolor=bg_color)
    gs = GridSpec(4, 3, height_ratios=[0.9, 1, 1, 1], hspace=0.26, wspace=0.10, figure=fig)
    draw_header(fig, gs[0, :], big=True)

    axes = [fig.add_subplot(gs[r + 1, c]) for r in range(3) for c in range(3)]
    for ax in axes:
        ax.set_facecolor(bg_color)
    for (name, fn), ax in zip(PANELS, axes):
        fn(ax)

    draw_footer(fig, y=0.02, size=20)
    path = os.path.join(out_dir, '00_match_dashboard_sofascore.png')
    fig.savefig(path, bbox_inches='tight', facecolor=bg_color, dpi=dpi)
    plt.close(fig)
    print(f'  wrote {os.path.basename(path)}')
    return path


def render_match(bundle_path: str, output_root: str | None = None, season: str | None = None,
                 matchweek: int | None = None, dpi: int = 150,
                 club: str | None = None, job_id: str | None = None) -> dict:
    """Render into the same season layout the WhoScored report uses."""
    from visualizer import output_layout

    prepare(bundle_path)
    match_path = output_layout.match_dir(
        season=season, date=M.date, home=M.hteamName, away=M.ateamName,
        matchweek=matchweek, competition=M.competition, output_root=output_root,
        club=club)

    render_individual(output_layout.panels_dir(match_path), dpi=dpi)
    render_dashboard(match_path)

    xg = M.stats.get('Expected goals')
    poss = M.stats.get('Ball possession')
    return output_layout.finalise(match_path, {
        'source': 'sofascore', 'club': club, 'job_id': job_id,
        'event_id': M.event_id,
        'season': season, 'matchweek': matchweek, 'competition': M.competition,
        'date': M.date, 'venue': M.venue,
        'home_team': M.hteamName, 'away_team': M.ateamName,
        'score': f'{M.hgoal}-{M.agoal}',
        'stats': {'xg': list(xg) if xg else None,
                  'possession': list(poss) if poss else None,
                  'shots': [sum(1 for s in M.shots if s['is_home']),
                            sum(1 for s in M.shots if not s['is_home'])]},
        'note': 'reduced report - SofaScore has no pass-by-pass event feed',
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle', required=True)
    ap.add_argument('--out', default=None, help='flat output dir (legacy)')
    ap.add_argument('--output-root', default=None)
    ap.add_argument('--season', default=None)
    ap.add_argument('--matchweek', type=int, default=None)
    ap.add_argument('--dpi', type=int, default=150)
    ap.add_argument('--club', default=None,
                    help='nest the output under output/<club>/ (default: flat)')
    ap.add_argument('--job', default=None, help='job id to record in match.json')
    args = ap.parse_args()

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        prepare(args.bundle)
        out = args.out
        render_individual(out, dpi=args.dpi)
        render_dashboard(out)
    else:
        result = render_match(args.bundle, output_root=args.output_root,
                              season=args.season, matchweek=args.matchweek, dpi=args.dpi,
                              club=args.club, job_id=args.job)
        out = result['match_dir']

    print(f'Match : {M.hteamName} {M.hgoal}-{M.agoal} {M.ateamName}')
    print(f'Source: SofaScore event {M.event_id} | {M.competition} | {M.date}')
    print(f'Data  : {len(M.shots)} shots, {sum(len(v) for v in M.heat.values())} touch points, '
          f'{len(M.players["home"]) + len(M.players["away"])} players')
    print(f'Output: {out}')
    print('Done.')


if __name__ == '__main__':
    main()
