#!/usr/bin/env python3
"""Animate the possession that led to a goal.

    python visualizer/goal_animation.py --job <id>            # every goal
    python visualizer/goal_animation.py --job <id> --goal 3   # just the third

What this is, and what it is not
--------------------------------
This animates the **ball and the players who touch it**, from the moment the
scoring team won possession to the moment the ball crossed the line. It is not
tracking data and does not pretend to be.

WhoScored's Opta feed records one actor per event - the player on the ball -
and nothing about the other twenty-one. `matchCentreData` carries `events`,
`playerIdNameDictionary` and `formations`, and the formation block holds
*nominal* slot positions on a 0-10 grid, which is a tactical shape rather than
anywhere anyone actually stood. Real 22-player movement needs a tracking feed
(Metrica, Second Spectrum, SkillCorner); no free source publishes one for the
current Premier League.

Drawing the other twenty-one from average positions would look exactly like
tracking data and be fiction at any given second, so this does not do it. A
player appears when they touch the ball, at the coordinate the feed actually
recorded, and not before. That is the same line the rest of this codebase
holds: the SofaScore report renders nine panels rather than faking eighteen.

The possession chain
--------------------
Segmenting the chain is the only subtle part. Walking back while the scoring
team holds the ball fails immediately, because the opposition's defensive
contacts are interleaved through the attack - a block, a challenge, a
deflection off a defender. In the Bournemouth goal that broke the first
attempt, Saliba's deflected `BallTouch` sits between the cross and the finish.

So the walk stops only at a *controlled* opposition event, and treats a
defensive contact as part of our attack rather than the end of it. That
function is also what `db/readiness.py` calls the missing `possession_chains`
dimension, so it is written to be useful on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import matplotlib
matplotlib.use('Agg')

import matplotlib.animation as animation                             # noqa: E402
import matplotlib.pyplot as plt                                      # noqa: E402
import numpy as np                                                   # noqa: E402
import pandas as pd                                                  # noqa: E402
from mplsoccer import Pitch                                          # noqa: E402

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger(__name__)

PROCESSED = os.path.join(ROOT, 'data', 'processed')

# The transformer scales Opta's 100x100 onto a real pitch, which is what every
# other panel draws on.
PITCH_LENGTH, PITCH_WIDTH = 105.0, 68.0

# Opta records each event in the *acting* team's attacking frame, so an
# opponent's defensive touch inside our attack arrives mirrored. Drawing both
# teams on one pitch means putting them in a common frame.
def mirror(x: float, y: float) -> tuple[float, float]:
    return PITCH_LENGTH - x, PITCH_WIDTH - y


# The segmentation rule lives in analytics.possession, which is also what
# builds the possession_sequences table. One implementation, so an animation
# and the warehouse can never disagree about where a move began.
from analytics.possession import (ALWAYS_SOFT, DEFENSIVE_CONTACT,  # noqa: E402,F401
                                  ends_possession)

# Events where the player is on the ball, so worth a marker and a name.
ON_BALL = {'Pass', 'TakeOn', 'Goal', 'SavedShot', 'MissedShots', 'ShotOnPost',
           'BallRecovery', 'BallTouch', 'Dispossessed', 'Carry'}


def seconds(event) -> float:
    return float(event['minute']) * 60 + float(event['second'])


def possession_chain(df: pd.DataFrame, goal_pos: int) -> pd.DataFrame:
    """Every event from the start of the scoring team's possession to the goal.

    `goal_pos` is a positional index into `df`, not a label.

    The rule itself is `analytics.possession.ends_possession`, shared with the
    builder for `possession_sequences`. The CSV here uses WhoScored's column
    names (`teamId`, `outcomeType`) where the warehouse uses the loaded ones,
    so the two keys are adapted rather than the rule being rewritten.
    """
    team = df.iloc[goal_pos]['teamId']

    def as_event(row):
        return {'team_id': row['teamId'], 'type': row['type'],
                'outcome': row['outcomeType']}

    i = goal_pos
    while i > 0:
        if not ends_possession(as_event(df.iloc[i - 1]), team):
            i -= 1
            continue
        break
    return df.iloc[i:goal_pos + 1].copy()


def waypoints(chain: pd.DataFrame, team_id) -> list[dict]:
    """The ball's path through the chain, as timed points.

    A pass moves the ball from (x, y) to (endX, endY), so both ends are
    waypoints. Everything else contributes the one place it happened. Events
    sharing a timestamp - the feed's resolution is a whole second - are spread
    across that second so the ball keeps moving rather than teleporting.
    """
    points: list[dict] = []
    for _, e in chain.iterrows():
        own = e['teamId'] == team_id
        x, y = (e['x'], e['y']) if own else mirror(e['x'], e['y'])
        t = seconds(e)
        points.append({'t': t, 'x': x, 'y': y, 'event': e, 'kind': 'start',
                       'own': own})
        if own and str(e['type']) == 'Pass' and not pd.isna(e.get('endX')):
            points.append({'t': t, 'x': e['endX'], 'y': e['endY'],
                           'event': e, 'kind': 'end', 'own': True})

    # Spread ties: give every waypoint a strictly increasing time.
    base = points[0]['t']
    for p in points:
        p['t'] -= base
    i = 0
    while i < len(points):
        j = i
        while j + 1 < len(points) and points[j + 1]['t'] == points[i]['t']:
            j += 1
        if j > i:
            # Share the gap to the next distinct timestamp, or 1s at the end.
            nxt = points[j + 1]['t'] if j + 1 < len(points) else points[i]['t'] + 1.0
            span = max(nxt - points[i]['t'], 0.6)
            for k in range(i, j + 1):
                points[k]['t'] += span * (k - i) / (j - i + 1)
        i = j + 1
    return points


def _interpolate(points: list[dict], t: float) -> tuple[float, float]:
    times = [p['t'] for p in points]
    if t <= times[0]:
        return points[0]['x'], points[0]['y']
    if t >= times[-1]:
        return points[-1]['x'], points[-1]['y']
    i = int(np.searchsorted(times, t)) - 1
    a, b = points[i], points[i + 1]
    span = b['t'] - a['t']
    f = 0.0 if span <= 0 else (t - a['t']) / span
    # Ease in/out so a pass leaves and arrives like a pass rather than a slide.
    f = f * f * (3 - 2 * f)
    return a['x'] + (b['x'] - a['x']) * f, a['y'] + (b['y'] - a['y']) * f


def describe(chain: pd.DataFrame, team_id) -> dict:
    goal = chain.iloc[-1]
    own = chain[chain['teamId'] == team_id]
    return {
        'scorer': goal['name'],
        'minute': int(goal['minute']),
        'second': int(goal['second']),
        'events': len(chain),
        'seconds': seconds(goal) - seconds(chain.iloc[0]),
        'players': int(own['name'].nunique()),
        'passes': int((own['type'] == 'Pass').sum()),
        'xt': float(own['xT'].fillna(0).sum()) if 'xT' in own else 0.0,
    }


def animate_goal(job_id: str, goal_pos: int, out_path: str, *,
                 fps: int = 12, speed: float = 1.0, hold: float = 2.0,
                 dpi: int = 90, trail: float = 4.0) -> dict:
    """Render one goal's build-up to `out_path` (.gif)."""
    meta_path = os.path.join(PROCESSED, job_id, 'metadata.json')
    with open(meta_path) as fh:
        meta = json.load(fh)
    df = pd.read_csv(os.path.join(PROCESSED, job_id, 'EventData.csv'))

    goal = df.iloc[goal_pos]
    team_id = goal['teamId']
    chain = possession_chain(df, goal_pos)
    facts = describe(chain, team_id)

    home_id = meta.get('home_team_id')
    attacking = meta['home_color'] if team_id == home_id else meta['away_color']
    defending = meta['away_color'] if team_id == home_id else meta['home_color']
    teams = {int(k): v for k, v in meta['teams'].items()}
    attacker_name = teams.get(int(team_id), 'Home')
    defender_name = next(v for k, v in teams.items() if k != int(team_id))

    points = waypoints(chain, team_id)
    duration = points[-1]['t']
    n_frames = max(int(duration / max(speed, 0.01) * fps), 1) + int(hold * fps)

    pitch = Pitch(pitch_type='uefa', pitch_color='#FFFFFF', line_color='#B8B8B8',
                  linewidth=1.4, goal_type='box', corner_arcs=True, pad_top=2)
    # grid() reserves real axes for the title and endnote, so a touch on the
    # touchline cannot end up drawn over the subtitle.
    fig, axs = pitch.grid(figheight=8.2, grid_height=0.76, title_height=0.16,
                          endnote_height=0.05, title_space=0.01, axis=False)
    ax, ax_title, ax_note = axs['pitch'], axs['title'], axs['endnote']
    fig.set_facecolor('#FFFFFF')

    ax_title.text(0.5, 0.62,
                  f'{attacker_name} — {facts["scorer"]}  '
                  f'{facts["minute"]}′{facts["second"]:02d}″',
                  ha='center', va='center', fontsize=20, fontweight='bold',
                  color='#111111')
    ax_title.text(0.5, 0.16,
                  f'{meta.get("league", "")} | GW {meta.get("gameweek", "")} | '
                  f'{meta.get("date", "")} | {attacker_name} v {defender_name}',
                  ha='center', va='center', fontsize=11, color='#666666')
    ax_note.text(0.5, 0.4,
                 f'{facts["events"]} events · {facts["seconds"]:.0f}s · '
                 f'{facts["players"]} players · {facts["passes"]} passes   ·   '
                 'ball and on-ball players only — event data, not tracking',
                 ha='center', va='center', fontsize=9.5, color='#8A8A8A')

    clock = ax_title.text(0.01, 0.62, '', ha='left', va='center', fontsize=16,
                          fontweight='bold', color='#333333', family='monospace')
    # The move's xT, revealed at the goal: how much the build-up actually
    # raised the chance of scoring, rather than just how it looked.
    caption = ax_title.text(0.99, 0.62, '', ha='right', va='center', fontsize=13,
                            color=attacking, fontweight='bold')

    # Which way the scoring team is kicking. Opta records every event in the
    # acting team's attacking frame, so after mirroring the opposition it is
    # always left to right.
    ax.annotate('', xy=(66, -2.6), xytext=(39, -2.6), annotation_clip=False,
                arrowprops=dict(arrowstyle='-|>', color='#BBBBBB', lw=1.6))
    ax.text(52.5, -4.6, 'attacking', ha='center', va='top', fontsize=9,
            color='#AAAAAA', clip_on=False)

    # Artists reused across frames.
    trail_line, = ax.plot([], [], color='#444444', lw=1.6, alpha=0.35, zorder=4)
    ball, = ax.plot([], [], 'o', ms=11, color='#111111', mec='#FFFFFF', mew=1.8,
                    zorder=9)
    touch_scatter = ax.scatter([], [], s=290, facecolor=attacking,
                               edgecolor='#FFFFFF', linewidth=1.6, alpha=0.9,
                               zorder=6)
    labels: list = []
    arrows: list = []
    # Low on the pitch: the play itself is almost always in the upper two
    # thirds, and the word must not sit on top of the move it is announcing.
    goal_flash = ax.text(52.5, 9, '', ha='center', va='center', fontsize=44,
                         fontweight='bold', color=attacking, alpha=0.0, zorder=12)

    ball_x: list[float] = []
    ball_y: list[float] = []
    ball_t: list[float] = []

    def clear_dynamic():
        for artist in labels + arrows:
            artist.remove()
        labels.clear()
        arrows.clear()

    def frame(i):
        t = min(i / fps * speed, duration)
        x, y = _interpolate(points, t)

        ball_t.append(t)
        ball_x.append(x)
        ball_y.append(y)
        keep = [k for k, tt in enumerate(ball_t) if tt >= t - trail]
        trail_line.set_data([ball_x[k] for k in keep], [ball_y[k] for k in keep])
        ball.set_data([x], [y])

        clear_dynamic()

        # Touches that have happened by now.
        done = [p for p in points if p['kind'] == 'start' and p['t'] <= t + 1e-9]
        if done:
            offsets = np.array([[p['x'], p['y']] for p in done])
            colours = [attacking if p['own'] else defending for p in done]
            alphas = [0.35 + 0.55 * (k == len(done) - 1) for k in range(len(done))]
            touch_scatter.set_offsets(offsets)
            touch_scatter.set_facecolor(colours)
            touch_scatter.set_alpha(None)
            touch_scatter.set_facecolor(
                [matplotlib.colors.to_rgba(c, a) for c, a in zip(colours, alphas)])

            for k, p in enumerate(done):
                shirt = p['event'].get('shirtNo')
                if pd.isna(shirt):
                    continue
                labels.append(ax.text(
                    p['x'], p['y'], f'{int(shirt)}', ha='center', va='center',
                    fontsize=9.5, fontweight='bold', zorder=7,
                    color='#FFFFFF' if k == len(done) - 1 else '#FFFFFFAA'))

            latest = done[-1]['event']
            if pd.notna(latest.get('name')):
                labels.append(ax.text(
                    done[-1]['x'], done[-1]['y'] + 3.2, str(latest['name']),
                    ha='center', va='bottom', fontsize=10.5, fontweight='bold',
                    color='#222222', zorder=8))

        # Passes already completed, left on the pitch as the shape of the move.
        for p in points:
            if p['kind'] != 'end' or p['t'] > t + 1e-9:
                continue
            e = p['event']
            arrows.append(ax.annotate(
                '', xy=(p['x'], p['y']), xytext=(e['x'], e['y']), zorder=3,
                arrowprops=dict(arrowstyle='-|>', color=attacking, alpha=0.28,
                                lw=1.7, shrinkA=6, shrinkB=6)))

        elapsed = seconds(chain.iloc[0]) + t
        clock.set_text(f'{int(elapsed // 60):02d}:{int(elapsed % 60):02d}')

        if t >= duration - 1e-9:
            goal_flash.set_text('GOAL')
            goal_flash.set_alpha(min(1.0, (i / fps * speed - duration + hold) / 0.4))
            caption.set_text(f'xT {facts["xt"]:+.3f}')
        return ()

    anim = animation.FuncAnimation(fig, frame, frames=n_frames, interval=1000 / fps,
                                   blit=False)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    anim.save(out_path, writer=animation.PillowWriter(fps=fps), dpi=dpi,
              savefig_kwargs={'facecolor': '#FFFFFF'})
    plt.close(fig)

    facts['path'] = out_path
    facts['frames'] = n_frames
    facts['size_mb'] = round(os.path.getsize(out_path) / 1e6, 1)
    return facts


def goals_in(df: pd.DataFrame, team: str | None = None) -> list[int]:
    positions = [df.index.get_loc(i) for i in df.index[df['isGoal'] == True]]
    if team:
        positions = [p for p in positions
                     if str(df.iloc[p]['teamName']).lower() == team.lower()]
    return positions


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--job', required=True)
    ap.add_argument('--team', default=None, help='only this team\'s goals')
    ap.add_argument('--goal', type=int, default=None,
                    help='1-based index into the selected goals')
    ap.add_argument('--out', default=None, help='output .gif (single goal only)')
    ap.add_argument('--out-dir', default=None)
    ap.add_argument('--fps', type=int, default=12)
    ap.add_argument('--speed', type=float, default=1.0,
                    help='>1 plays faster than real time')
    ap.add_argument('--dpi', type=int, default=90)
    ap.add_argument('--min-events', type=int, default=3,
                    help='skip goals whose build-up is shorter than this')
    ap.add_argument('--force', action='store_true',
                    help='render even a goal with no build-up')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(levelname)-7s %(message)s')

    df = pd.read_csv(os.path.join(PROCESSED, a.job, 'EventData.csv'))
    positions = goals_in(df, a.team)
    if not positions:
        print(f'no goals{" for " + a.team if a.team else ""} in job {a.job}',
              file=sys.stderr)
        return 1
    if a.goal:
        positions = [positions[a.goal - 1]]

    from visualizer.output_layout import slugify

    out_dir = a.out_dir or os.path.join(ROOT, 'data', 'goal_animations', a.job)
    for n, pos in enumerate(positions, 1):
        g = df.iloc[pos]

        # A penalty, a direct free kick, or a shot straight off a turnover has
        # no build-up to show. Twenty of the season's goals are like this;
        # a one-frame GIF of the finish is not worth writing.
        chain = possession_chain(df, pos)
        if len(chain) < a.min_events and not a.force:
            print(f'  {int(g["minute"])}′ {str(g["name"]):22s} '
                  f'{len(chain):3d} events  — skipped, no build-up to animate '
                  f'(--force to render anyway)')
            continue

        slug = f'{int(g["minute"]):02d}m-{slugify(g["name"])}'
        out = a.out if (a.out and len(positions) == 1) \
            else os.path.join(out_dir, f'goal-{n}-{slug}.gif')
        facts = animate_goal(a.job, pos, out, fps=a.fps, speed=a.speed, dpi=a.dpi)
        print(f'  {facts["minute"]}′ {facts["scorer"]:22s} '
              f'{facts["events"]:3d} events  {facts["seconds"]:5.0f}s  '
              f'{facts["players"]} players  xT {facts["xt"]:+.3f}  -> '
              f'{facts["path"]} ({facts["size_mb"]} MB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
