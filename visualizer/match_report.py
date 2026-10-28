#!/usr/bin/env python3
"""
Full post-match report generator - faithful port of Post_Match_Report.ipynb.

Reproduces all 18 visuals of the notebook dashboard from a scraped WhoScored
match JSON, and writes them both as individual PNGs and as one unified
6x3 dashboard with both club crests in the header.

Usage:
    python visualizer/match_report.py                     # newest scraped match
    python visualizer/match_report.py --job <job_id>
    python visualizer/match_report.py --out final_output
"""

import argparse
import ast
import json
import logging
import os
import sys
import warnings
from functools import lru_cache
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from visualizer.social_export import export_social_jpg, make_png_upload_safe  # noqa: E402

import matplotlib
matplotlib.use('Agg')

import matplotlib.image as mpimg
import matplotlib.patches as patches
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, to_rgba
from matplotlib.gridspec import GridSpec
from mplsoccer import Pitch

warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, 'visualizer', 'assets')

# Notebook palette
green = '#69f900'
violet = '#a369ff'
bg_color = '#FFFFFF'
line_color = '#000000'

muted_color = '#444444'
faint_color = '#777777'
credit_color = '#999999'
accent_color = line_color

path_eff = [path_effects.Stroke(linewidth=3, foreground=bg_color), path_effects.Normal()]

# Every panel reads these module globals at draw time, and prepare() builds the
# heatmap colormaps from bg_color, so apply_theme() must run before prepare().
THEMES = {
    'classic': dict(bg_color='#FFFFFF', line_color='#000000', muted_color='#444444',
                    faint_color='#777777', credit_color='#999999', accent_color='#000000'),
    # Champions League: white page, UCL navy lines and rules, competition logo in the header.
    'ucl': dict(bg_color='#FFFFFF', line_color='#0A1747', muted_color='#3A4573',
                faint_color='#6B759C', credit_color='#8E9BC9', accent_color='#0A1747'),
}


def apply_theme(name: str = 'classic') -> None:
    global bg_color, line_color, muted_color, faint_color, credit_color, accent_color, path_eff
    t = THEMES[name]
    bg_color, line_color = t['bg_color'], t['line_color']
    muted_color, faint_color = t['muted_color'], t['faint_color']
    credit_color, accent_color = t['credit_color'], t['accent_color']
    path_eff = [path_effects.Stroke(linewidth=3, foreground=bg_color), path_effects.Normal()]

# Populated by prepare(); the notebook uses module level globals, so does this.
M = SimpleNamespace()


# --------------------------------------------------------------------------
# Data loading / preprocessing  (notebook cells 7 & 8)
# --------------------------------------------------------------------------

def _qualifier_str(q):
    """Reproduce the str() form the notebook gets from its CSV round trip, so
    that every `qualifiers.str.contains(...)` filter behaves identically."""
    return str(q)


def latest_job():
    proc = os.path.join(ROOT, 'data', 'processed')
    jobs = [d for d in os.listdir(proc) if os.path.isdir(os.path.join(proc, d))]
    jobs = [j for j in jobs if os.path.exists(os.path.join(ROOT, 'data', 'raw', f'match_{j}.json'))]
    if not jobs:
        raise SystemExit('No scraped match JSON found under data/raw/')
    return max(jobs, key=lambda j: os.path.getmtime(os.path.join(ROOT, 'data', 'raw', f'match_{j}.json')))


def _meta_has_xg(meta: dict) -> bool:
    """True when team xG/xGOT should appear on panels and the dashboard header.

    WhoScored never supplies xG; zeros are the transform default. SofaScore may
    mark the feed unavailable (e.g. Community Shield). Hide rather than show 0.0.
    """
    src = str(meta.get('xg_source') or '')
    if 'unavailable' in src.lower():
        return False
    try:
        hxg = float(meta['home_xg']) if meta.get('home_xg') is not None else None
        axg = float(meta['away_xg']) if meta.get('away_xg') is not None else None
        hxgot = float(meta['home_xgot']) if meta.get('home_xgot') is not None else None
        axgot = float(meta['away_xgot']) if meta.get('away_xgot') is not None else None
    except (TypeError, ValueError):
        return False
    if hxg is None and axg is None:
        return False
    if (hxg or 0.0) == 0.0 and (axg or 0.0) == 0.0 and (hxgot or 0.0) == 0.0 and (axgot or 0.0) == 0.0:
        # All-zero with no confirmed source = placeholder, not a measured 0–0.
        return bool(src) and any(s in src.lower() for s in ('sofascore', 'fotmob'))
    return True


def load_match(job_id):
    raw_path = os.path.join(ROOT, 'data', 'raw', f'match_{job_id}.json')
    with open(raw_path) as fh:
        data = json.load(fh)
    mcd = data['matchCentreData']

    meta_path = os.path.join(ROOT, 'data', 'processed', job_id, 'metadata.json')
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path) as fh:
            meta = json.load(fh)
    return data, mcd, meta


def prepare(job_id):
    data, mcd, meta = load_match(job_id)

    events_dict = mcd['events']
    teams_dict = {mcd['home']['teamId']: mcd['home']['name'],
                  mcd['away']['teamId']: mcd['away']['name']}

    players_home_df = pd.DataFrame(mcd['home']['players'])
    players_home_df['teamId'] = mcd['home']['teamId']
    players_away_df = pd.DataFrame(mcd['away']['players'])
    players_away_df['teamId'] = mcd['away']['teamId']
    players_df = pd.concat([players_home_df, players_away_df], ignore_index=True)

    # ---- event dataframe -------------------------------------------------
    df = pd.DataFrame(events_dict)
    df['type'] = df['type'].apply(lambda v: v['displayName'])
    df['outcomeType'] = df['outcomeType'].apply(lambda v: v['displayName'])
    df['period'] = df['period'].apply(lambda v: v['displayName'])
    df['qualifiers'] = df['qualifiers'].apply(_qualifier_str)
    for col in ('endX', 'endY', 'goalMouthZ', 'goalMouthY', 'blockedX', 'blockedY',
                'isShot', 'isGoal', 'isTouch', 'playerId'):
        if col not in df.columns:
            df[col] = np.nan
    df = df.reset_index(drop=True)
    df['row'] = df.index

    # ---- xT (notebook: mckayjohns 12x8 grid, on successful open play passes)
    xt_path = os.path.join(ASSETS, 'xT_Grid.csv')
    xT = np.array(pd.read_csv(xt_path, header=None))
    xT_rows, xT_cols = xT.shape

    dfxT = df.copy()
    dfxT = dfxT[~dfxT['qualifiers'].str.contains('Corner') & ~dfxT['qualifiers'].str.contains('ThrowIn')]
    dfxT = dfxT[(dfxT['type'] == 'Pass') & (dfxT['outcomeType'] == 'Successful')]

    dfxT['x1_bin_xT'] = pd.cut(dfxT['x'], bins=xT_cols, labels=False)
    dfxT['y1_bin_xT'] = pd.cut(dfxT['y'], bins=xT_rows, labels=False)
    dfxT['x2_bin_xT'] = pd.cut(dfxT['endX'], bins=xT_cols, labels=False)
    dfxT['y2_bin_xT'] = pd.cut(dfxT['endY'], bins=xT_rows, labels=False)

    ok = dfxT[['x1_bin_xT', 'y1_bin_xT', 'x2_bin_xT', 'y2_bin_xT']].notna().all(axis=1)
    dfxT = dfxT[ok].copy()
    for c in ('x1_bin_xT', 'y1_bin_xT', 'x2_bin_xT', 'y2_bin_xT'):
        dfxT[c] = dfxT[c].astype(int)

    dfxT['start_zone_value_xT'] = xT[dfxT['y1_bin_xT'].to_numpy(), dfxT['x1_bin_xT'].to_numpy()]
    dfxT['end_zone_value_xT'] = xT[dfxT['y2_bin_xT'].to_numpy(), dfxT['x2_bin_xT'].to_numpy()]
    dfxT['xT'] = dfxT['end_zone_value_xT'] - dfxT['start_zone_value_xT']

    df = df.merge(dfxT[['row', 'start_zone_value_xT', 'end_zone_value_xT', 'xT']], on='row', how='left')
    df['teamName'] = df['teamId'].map(teams_dict)

    # ---- reshape 100x100 -> 105x68 --------------------------------------
    df['x'] = df['x'] * 1.05
    df['y'] = df['y'] * 0.68
    df['endX'] = df['endX'] * 1.05
    df['endY'] = df['endY'] * 0.68
    df['goalMouthY'] = df['goalMouthY'] * 0.68

    dfp = players_df[['playerId', 'shirtNo', 'name', 'position', 'isFirstEleven']].copy()
    df = df.merge(dfp, on='playerId', how='left')

    # ---- progressive pass distance gain ----------------------------------
    df['pro'] = np.where(
        (df['type'] == 'Pass') & (df['outcomeType'] == 'Successful') & (df['x'] > 42),
        np.sqrt((105 - df['x']) ** 2 + (34 - df['y']) ** 2) - np.sqrt((105 - df['endX']) ** 2 + (34 - df['endY']) ** 2),
        0)

    df['shortName'] = df['name'].apply(get_short_name)

    # ---- match level variables (notebook cell 8) -------------------------
    hteamID, ateamID = list(teams_dict.keys())
    hteamName, ateamName = teams_dict[hteamID], teams_dict[ateamID]

    hcol = meta.get('home_color', '#1B458F')
    acol = meta.get('away_color', '#EF0107')

    homedf = df[df['teamId'] == hteamID]
    awaydf = df[df['teamId'] == ateamID]

    hgoal_count = len(homedf[(homedf['type'] == 'Goal') & (~homedf['qualifiers'].str.contains('OwnGoal'))]) \
        + len(awaydf[(awaydf['type'] == 'Goal') & (awaydf['qualifiers'].str.contains('OwnGoal'))])
    agoal_count = len(awaydf[(awaydf['type'] == 'Goal') & (~awaydf['qualifiers'].str.contains('OwnGoal'))]) \
        + len(homedf[(homedf['type'] == 'Goal') & (homedf['qualifiers'].str.contains('OwnGoal'))])

    M.__dict__.update(dict(
        job_id=job_id, data=data, mcd=mcd, meta=meta,
        events_dict=events_dict, players_df=players_df, teams_dict=teams_dict,
        df=df, homedf=homedf, awaydf=awaydf,
        hteamID=hteamID, ateamID=ateamID, hteamName=hteamName, ateamName=ateamName,
        home_team_id=hteamID, away_team_id=ateamID,
        hcol=hcol, acol=acol,
        hgoal_count=hgoal_count, agoal_count=agoal_count,
        hxT=round(float(homedf['xT'].sum()), 2), axT=round(float(awaydf['xT'].sum()), 2),
        # xG / xGOT are not present in WhoScored data - taken from metadata.json
        # (the notebook copies them from Fotmob by hand). Omit from panels when
        # SofaScore (or manual override) did not supply real values.
        hxg=meta.get('home_xg', 0.0), axg=meta.get('away_xg', 0.0),
        hxgot=meta.get('home_xgot', 0.0), axgot=meta.get('away_xgot', 0.0),
        has_xg=_meta_has_xg(meta),
        league=meta.get('league', ''), gw=meta.get('gameweek', ''),
        date=(mcd.get('startDate') or mcd.get('startTime') or '')[:10],
        venue=mcd.get('venueName', ''), referee=(mcd.get('referee') or {}).get('name', ''),
        attendance=mcd.get('attendance'),
        pearl_cmaph=LinearSegmentedColormap.from_list('h', [bg_color, hcol], N=20),
        pearl_cmapa=LinearSegmentedColormap.from_list('a', [bg_color, acol], N=20),
    ))

    build_frames()
    return M


def get_short_name(full_name):
    if not isinstance(full_name, str):
        return full_name
    parts = full_name.split()
    if len(parts) == 1:
        return full_name
    if len(parts) == 2:
        return parts[0][0] + '. ' + parts[1]
    return parts[0][0] + '. ' + parts[1][0] + '. ' + ' '.join(parts[2:])


# --------------------------------------------------------------------------
# Derived frames used by the plots
# --------------------------------------------------------------------------

def build_frames():
    df, events_dict, players_df = M.df, M.events_dict, M.players_df
    hteamID, ateamID = M.hteamID, M.ateamID

    # -- pass network ------------------------------------------------------
    M.passes_df = get_passes_df(events_dict)
    M.home_passes_between_df, M.home_average_locs_and_count_df = get_passes_between_df(hteamID)
    M.away_passes_between_df, M.away_average_locs_and_count_df = get_passes_between_df(ateamID)

    # -- defensive block ---------------------------------------------------
    M.defensive_actions_df = get_defensive_action_df(events_dict)
    dh = get_da_count_df(hteamID)
    da = get_da_count_df(ateamID)
    M.defensive_home_average_locs_and_count_df = dh[dh['position'] != 'GK']
    M.defensive_away_average_locs_and_count_df = da[da['position'] != 'GK']

    # -- progressive passes (corners excluded) -----------------------------
    M.dfhpp = df[(df['teamId'] == hteamID) & (df['type'] == 'Pass') & (~df['qualifiers'].str.contains('Corner'))]
    M.dfapp = df[(df['teamId'] == ateamID) & (df['type'] == 'Pass') & (~df['qualifiers'].str.contains('Corner'))]

    # -- danger zone passes (successful, no corners) -----------------------
    M.dfhp = df[(df['teamId'] == hteamID) & (df['type'] == 'Pass') & (df['outcomeType'] == 'Successful')
                & (~df['qualifiers'].str.contains('Corner'))]
    M.dfap = df[(df['teamId'] == ateamID) & (df['type'] == 'Pass') & (df['outcomeType'] == 'Successful')
                & (~df['qualifiers'].str.contains('Corner'))]

    # -- pass end zone -----------------------------------------------------
    M.hdfPEZ = df[(df['teamId'] == hteamID) & (df['type'] == 'Pass') & (df['outcomeType'] == 'Successful')]
    M.adfPEZ = df[(df['teamId'] == ateamID) & (df['type'] == 'Pass') & (df['outcomeType'] == 'Successful')]

    # -- chance creating zone ----------------------------------------------
    M.dfchch = df[(df['teamId'] == hteamID) & (df['qualifiers'].str.contains('KeyPass', na=False))]
    M.dfchca = df[(df['teamId'] == ateamID) & (df['qualifiers'].str.contains('KeyPass', na=False))]

    # -- shots -------------------------------------------------------------
    mask4 = df['type'].isin(['Goal', 'MissedShots', 'SavedShot', 'ShotOnPost'])
    Shotsdf = df[mask4].reset_index(drop=True)
    M.Shotsdf = Shotsdf
    hShotsdf = Shotsdf[Shotsdf['teamId'] == hteamID]
    aShotsdf = Shotsdf[Shotsdf['teamId'] == ateamID]
    M.hSavedf = hShotsdf[(hShotsdf['type'] == 'SavedShot') & (~hShotsdf['qualifiers'].str.contains(': 82,'))]
    M.aSavedf = aShotsdf[(aShotsdf['type'] == 'SavedShot') & (~aShotsdf['qualifiers'].str.contains(': 82,'))]
    M.hogdf = hShotsdf[hShotsdf['qualifiers'].str.contains('OwnGoal')]
    M.aogdf = aShotsdf[aShotsdf['qualifiers'].str.contains('OwnGoal')]

    M.hTotalShots, M.aTotalShots = len(hShotsdf), len(aShotsdf)
    M.hShotsOnT = len(M.hSavedf) + M.hgoal_count
    M.aShotsOnT = len(M.aSavedf) + M.agoal_count
    # NOTE: the notebook divides the away xG by the *home* shot count here; fixed.
    M.hxGpSh = round(M.hxg / M.hTotalShots, 2) if M.hTotalShots else 0.0
    M.axGpSh = round(M.axg / M.aTotalShots, 2) if M.aTotalShots else 0.0
    goal = (105, 34)
    M.home_average_shot_distance = round(float(np.sqrt((hShotsdf['x'] - goal[0]) ** 2 + (hShotsdf['y'] - goal[1]) ** 2).mean()), 2)
    M.away_average_shot_distance = round(float(np.sqrt((aShotsdf['x'] - goal[0]) ** 2 + (aShotsdf['y'] - goal[1]) ** 2).mean()), 2)

    # -- momentum ----------------------------------------------------------
    mom = df[['minute', 'teamId', 'start_zone_value_xT']].copy()
    mom.loc[mom['teamId'] == ateamID, 'start_zone_value_xT'] *= -1
    mom = mom.groupby('minute')['start_zone_value_xT'].mean().reset_index()
    mom.columns = ['minute', 'average_xT']
    mom['average_xT'] = mom['average_xT'].fillna(0)
    M.Momentumdf = mom

    compute_team_stats()


def compute_team_stats():
    df, h, a = M.df, M.hteamID, M.ateamID

    def n(team, cond):
        return int(((df['teamId'] == team) & cond).sum())

    is_pass = df['type'] == 'Pass'
    succ = df['outcomeType'] == 'Successful'
    q = df['qualifiers']

    hposs_n, aposs_n = n(h, is_pass), n(a, is_pass)
    M.hposs = round(hposs_n / (hposs_n + aposs_n) * 100, 2)
    M.aposs = round(aposs_n / (hposs_n + aposs_n) * 100, 2)

    ft = (df['isTouch'] == True) & (df['x'] >= 70)  # noqa: E712
    hft_n, aft_n = n(h, ft), n(a, ft)
    M.hft = round(hft_n / (hft_n + aft_n) * 100, 2)
    M.aft = round(aft_n / (hft_n + aft_n) * 100, 2)

    M.htotalPass, M.atotalPass = hposs_n, aposs_n
    M.hAccPass, M.aAccPass = n(h, is_pass & succ), n(a, is_pass & succ)

    lb = is_pass & q.str.contains('Longball') & ~q.str.contains('Corner') & ~q.str.contains('Cross')
    M.hLongB, M.aLongB = n(h, lb), n(a, lb)
    M.hAccLongB, M.aAccLongB = n(h, lb & succ), n(a, lb & succ)

    cr = is_pass & q.str.contains('Cross')
    M.hCrss, M.aCrss = n(h, cr), n(a, cr)
    M.hAccCrss, M.aAccCrss = n(h, cr & succ), n(a, cr & succ)

    M.hCor, M.aCor = n(h, is_pass & q.str.contains('Corner')), n(a, is_pass & q.str.contains('Corner'))
    M.htins, M.atins = n(h, is_pass & q.str.contains('ThrowIn')), n(a, is_pass & q.str.contains('ThrowIn'))

    drb = (df['type'] == 'TakeOn') & q.str.contains('Offensive')
    M.htotalDrb, M.atotalDrb = n(h, drb), n(a, drb)
    M.hAccDrb, M.aAccDrb = n(h, drb & succ), n(a, drb & succ)

    # goal kick length
    def extract_length(qs):
        for item in ast.literal_eval(qs):
            if item['type']['displayName'] == 'Length':
                return float(item['value'])
        return np.nan

    gk = is_pass & q.str.contains('GoalKick')
    hgk = df[(df['teamId'] == h) & gk]['qualifiers'].apply(extract_length)
    agk = df[(df['teamId'] == a) & gk]['qualifiers'].apply(extract_length)
    M.hglkl = round(float(hgk.mean()), 2) if len(hgk) else 0.0
    M.aglkl = round(float(agk.mean()), 2) if len(agk) else 0.0

    for key, etype in [('tkl', 'Tackle'), ('intc', 'Interception'), ('clr', 'Clearance'),
                       ('arl', 'Aerial'), ('blrc', 'BallRecovery'), ('blkp', 'BlockedPass'),
                       ('ofs', 'OffsideGiven'), ('foul', 'Foul')]:
        cond = df['type'] == etype
        setattr(M, 'h' + key, n(h, cond))
        setattr(M, 'a' + key, n(a, cond))
    M.htklw, M.atklw = n(h, (df['type'] == 'Tackle') & succ), n(a, (df['type'] == 'Tackle') & succ)
    M.harlw, M.aarlw = n(h, (df['type'] == 'Aerial') & succ), n(a, (df['type'] == 'Aerial') & succ)


# --------------------------------------------------------------------------
# 1 & 3 - Pass network
# --------------------------------------------------------------------------

def get_passes_df(events_dict):
    d = pd.DataFrame(events_dict)
    d['eventType'] = d['type'].apply(lambda v: v['displayName'])
    d['outcomeType'] = d['outcomeType'].apply(lambda v: v['displayName'])
    d['receiver'] = d['playerId'].shift(-1)
    ids = d.index[d['eventType'] == 'Pass']
    return d.loc[ids, ['id', 'x', 'y', 'endX', 'endY', 'teamId', 'playerId', 'receiver',
                       'eventType', 'outcomeType']]


def get_passes_between_df(team_id):
    """Median event position per player + completed-pass volume per pair.

    Coordinates stay in raw opta (0-100) units, matching the notebook.
    Only *successful* passes build the edges - an unsuccessful pass has no
    receiver, so counting it would invent a link that never happened.
    """
    players_df = M.players_df
    passes_df = M.passes_df
    passes_df = passes_df[(passes_df['teamId'] == team_id) & (passes_df['outcomeType'] == 'Successful')]

    d = pd.DataFrame(M.events_dict)
    dteam = d[d['teamId'] == team_id]

    average_locs_and_count_df = dteam.groupby('playerId').agg({'x': ['median'], 'y': ['median', 'count']})
    average_locs_and_count_df.columns = ['pass_avg_x', 'pass_avg_y', 'count']
    average_locs_and_count_df = average_locs_and_count_df.merge(
        players_df[['playerId', 'name', 'shirtNo', 'position', 'isFirstEleven']],
        on='playerId', how='left').set_index('playerId')

    ids = passes_df.loc[:, ['id', 'playerId', 'receiver', 'teamId']].copy()
    ids['pos_max'] = ids[['playerId', 'receiver']].max(axis='columns')
    ids['pos_min'] = ids[['playerId', 'receiver']].min(axis='columns')

    passes_between_df = ids.groupby(['pos_min', 'pos_max']).id.count().reset_index()
    passes_between_df.rename({'id': 'pass_count'}, axis='columns', inplace=True)
    passes_between_df = passes_between_df.merge(average_locs_and_count_df, left_on='pos_min', right_index=True)
    passes_between_df = passes_between_df.merge(average_locs_and_count_df, left_on='pos_max',
                                                right_index=True, suffixes=['', '_end'])
    return passes_between_df, average_locs_and_count_df


def pass_network_visualization(ax, passes_between_df, average_locs_and_count_df, col, team_id):
    MAX_LINE_WIDTH = 15
    MIN_TRANSPARENCY, MAX_TRANSPARENCY = 0.05, 0.85

    passes_between_df = passes_between_df.copy()
    passes_between_df['width'] = passes_between_df.pass_count / passes_between_df.pass_count.max() * MAX_LINE_WIDTH

    color = np.tile(np.array(to_rgba(col)), (len(passes_between_df), 1))
    c_transparency = passes_between_df.pass_count / passes_between_df.pass_count.max()
    color[:, 3] = c_transparency * (MAX_TRANSPARENCY - MIN_TRANSPARENCY) + MIN_TRANSPARENCY

    pitch = Pitch(pitch_type='opta', goal_type='box', goal_alpha=.5, corner_arcs=True,
                  pitch_color=bg_color, line_color=line_color, linewidth=2)
    pitch.draw(ax=ax)

    pitch.lines(passes_between_df.pass_avg_x, passes_between_df.pass_avg_y,
                passes_between_df.pass_avg_x_end, passes_between_df.pass_avg_y_end,
                lw=passes_between_df.width, color=color, zorder=1, ax=ax)

    for _, row in average_locs_and_count_df.iterrows():
        if row['isFirstEleven'] is True:
            pitch.scatter(row['pass_avg_x'], row['pass_avg_y'], s=1000, marker='o', color=bg_color,
                          edgecolor=line_color, linewidth=2, alpha=1, zorder=2, ax=ax)
        else:
            pitch.scatter(row['pass_avg_x'], row['pass_avg_y'], s=1000, marker='s', color=bg_color,
                          edgecolor=line_color, linewidth=2, alpha=0.75, zorder=2, ax=ax)

    for _, row in average_locs_and_count_df.iterrows():
        pitch.annotate(row['shirtNo'], xy=(row.pass_avg_x, row.pass_avg_y), c=col, ha='center',
                       va='center', size=18, zorder=3, ax=ax)

    avgph = round(average_locs_and_count_df['pass_avg_x'].median(), 2)
    avgph_show = round(avgph * 1.05, 2)
    ax.axvline(x=avgph, color='gray', linestyle='--', alpha=0.75, linewidth=2)

    total = int(passes_between_df.pass_count.sum())
    if team_id == M.away_team_id:
        ax.invert_xaxis()
        ax.invert_yaxis()
        ax.text(avgph + 1, 105, f'{avgph_show}m', fontsize=15, color=line_color, ha='right')
        ax.text(2, 2, 'circle = starter\nbox = sub', color=M.acol, size=12, ha='right', va='top')
        ax.text(-2, 105, '<--- Attacking Direction', color=M.acol, size=15, ha='right', va='center')
        ax.set_title(f'{M.ateamName} Passing Network', color=line_color, size=30,
                     fontweight='bold', path_effects=path_eff)
        sub_y = -2
    else:
        ax.text(avgph + 1, -5, f'{avgph_show}m', fontsize=15, color=line_color, ha='left')
        ax.text(2, 98, 'circle = starter\nbox = sub', color=M.hcol, size=12, ha='left', va='top')
        ax.text(-2, -5, 'Attacking Direction --->', color=M.hcol, size=15, ha='left', va='center')
        ax.set_title(f'{M.hteamName} Passing Network', color=line_color, size=30,
                     fontweight='bold', path_effects=path_eff)
        sub_y = 102

    ax.text(50, sub_y, f'{total} completed passes  |  avg. line height {avgph_show}m',
            color='gray', size=13, ha='center', va='center')
    return pitch


# --------------------------------------------------------------------------
# 4 & 6 - Defensive block
# --------------------------------------------------------------------------

def get_defensive_action_df(events_dict):
    d = pd.DataFrame(events_dict)
    d['eventType'] = d['type'].apply(lambda v: v['displayName'])
    d['outcomeType'] = d['outcomeType'].apply(lambda v: v['displayName'])
    ids = d.index[((d['eventType'] == 'Aerial') & (d['x'] <= 80)) |
                  (d['eventType'] == 'BallRecovery') |
                  (d['eventType'] == 'BlockedPass') |
                  (d['eventType'] == 'Challenge') |
                  (d['eventType'] == 'Clearance') |
                  (d['eventType'] == 'Error') |
                  (d['eventType'] == 'Foul') |
                  (d['eventType'] == 'Interception') |
                  (d['eventType'] == 'Tackle')]
    return d.loc[ids, ['id', 'x', 'y', 'teamId', 'playerId', 'eventType', 'outcomeType']]


def get_da_count_df(team_id):
    dad = M.defensive_actions_df
    dad = dad[dad['teamId'] == team_id]
    out = dad.groupby('playerId').agg({'x': ['median'], 'y': ['median', 'count']})
    out.columns = ['x', 'y', 'count']
    out = out.merge(M.players_df[['playerId', 'name', 'shirtNo', 'position', 'isFirstEleven']],
                    on='playerId', how='left').set_index('playerId')
    return out


def defensive_block(ax, average_locs_and_count_df, team_id, col):
    dat = M.defensive_actions_df[M.defensive_actions_df['teamId'] == team_id]
    pitch = Pitch(pitch_type='opta', pitch_color=bg_color, line_color=line_color, linewidth=2,
                  line_zorder=2, corner_arcs=True, goal_type='box', goal_alpha=.5)
    pitch.draw(ax=ax)
    ax.set_facecolor(bg_color)

    average_locs_and_count_df = average_locs_and_count_df.copy()
    MAX_MARKER_SIZE = 3500
    average_locs_and_count_df['marker_size'] = (average_locs_and_count_df['count'] /
                                                average_locs_and_count_df['count'].max() * MAX_MARKER_SIZE)

    flamingo_cmap = LinearSegmentedColormap.from_list('Flamingo', [bg_color, col], N=500)
    pitch.kdeplot(dat.x, dat.y, ax=ax, fill=True, levels=500, thresh=0.02, cut=4, cmap=flamingo_cmap)

    for _, row in average_locs_and_count_df.iterrows():
        marker = 'o' if row['isFirstEleven'] is True else 's'
        pitch.scatter(row['x'], row['y'], s=row['marker_size'] + 100, marker=marker, color=bg_color,
                      edgecolor=line_color, linewidth=1, alpha=1, zorder=3, ax=ax)

    pitch.scatter(dat.x, dat.y, s=10, marker='x', color='orange', alpha=0.4, zorder=4, ax=ax)

    for _, row in average_locs_and_count_df.iterrows():
        pitch.annotate(row['shirtNo'], xy=(row.x, row.y), c=line_color, ha='center', va='center',
                       size=14, zorder=5, ax=ax)

    dah = round(average_locs_and_count_df['x'].mean(), 2)
    dah_show = round(dah * 1.05, 2)
    ax.axvline(x=dah, color='gray', linestyle='--', alpha=0.75, linewidth=2)

    if team_id == M.ateamID:
        ax.invert_xaxis()
        ax.invert_yaxis()
        ax.text(dah + 1, 105, f'{dah_show}m', fontsize=15, color=line_color, ha='right', va='center')
        ax.text(2, 2, 'circle = starter\nbox = sub', color='gray', size=12, ha='right', va='top')
        ax.text(-2, 105, '<--- Attacking Direction', color=M.acol, size=15, ha='right', va='center')
        ax.set_title(f'{M.ateamName} Defensive Block', color=line_color, fontsize=30,
                     fontweight='bold', path_effects=path_eff)
        sub_y = -2
    else:
        ax.text(dah + 1, -5, f'{dah_show}m', fontsize=15, color=line_color, ha='left', va='center')
        ax.text(2, 98, 'circle = starter\nbox = sub', color='gray', size=12, ha='left', va='top')
        ax.text(-2, -5, 'Attacking Direction --->', color=M.hcol, size=15, ha='left', va='center')
        ax.set_title(f'{M.hteamName} Defensive Block', color=line_color, fontsize=30,
                     fontweight='bold', path_effects=path_eff)
        sub_y = 102

    ax.text(50, sub_y, f'{len(dat)} defensive actions  |  avg. height {dah_show}m',
            color='gray', size=13, ha='center', va='center')
    return pitch


# --------------------------------------------------------------------------
# 7 & 9 - Progressive passes
# --------------------------------------------------------------------------

def draw_progressive_pass_map(ax, df, team_name, col):
    pitch = Pitch(pitch_type='uefa', pitch_color=bg_color, line_color=line_color, linewidth=2,
                  corner_arcs=True, goal_type='box', goal_alpha=.5)
    pitch.draw(ax=ax)
    if team_name == M.ateamName:
        ax.invert_xaxis()
        ax.invert_yaxis()

    dfpro = df[df['pro'] >= 9.144]
    pro_count = len(dfpro)

    left_pro = len(dfpro[dfpro['y'] >= 45.33])
    mid_pro = len(dfpro[(dfpro['y'] >= 22.67) & (dfpro['y'] < 45.33)])
    right_pro = len(dfpro[(dfpro['y'] >= 0) & (dfpro['y'] < 22.67)])
    pct = (lambda v: int(v / pro_count * 100) if pro_count else 0)

    ax.hlines(22.67, xmin=0, xmax=105, colors=line_color, linestyle='dashed', alpha=0.35)
    ax.hlines(45.33, xmin=0, xmax=105, colors=line_color, linestyle='dashed', alpha=0.35)

    ax.text(27, 11.335, f'{right_pro}\n({pct(right_pro)}%)', color=col, fontsize=24, va='center', ha='center')
    ax.text(27, 34, f'{mid_pro}\n({pct(mid_pro)}%)', color=col, fontsize=24, va='center', ha='center')
    ax.text(27, 56.675, f'{left_pro}\n({pct(left_pro)}%)', color=col, fontsize=24, va='center', ha='center')

    pitch.lines(dfpro.x, dfpro.y, dfpro.endX, dfpro.endY, lw=3.5, transparent=True, comet=True,
                color=col, ax=ax, alpha=0.5)
    pitch.scatter(dfpro.endX, dfpro.endY, s=35, edgecolor=col, linewidth=1, color=bg_color, zorder=2, ax=ax)

    if team_name == M.ateamName:
        ax.text(0, 73, '<--- Attacking Direction', color=M.acol, size=15, ha='right', va='center')
    else:
        ax.text(0, -5, 'Attacking Direction --->', color=M.hcol, size=15, ha='left', va='center')
    ax.set_title(f'{team_name}: {pro_count} Progressive Passes', color=line_color, fontsize=30,
                 fontweight='bold', path_effects=path_eff)


# --------------------------------------------------------------------------
# 10 & 12 - Zone 14 & half space passes
# --------------------------------------------------------------------------

def draw_pass_map(ax, df, team_name, col):
    pitch = Pitch(pitch_type='uefa', pitch_color=bg_color, line_color=line_color, linewidth=2,
                  corner_arcs=True, goal_type='box', goal_alpha=.5)
    pitch.draw(ax=ax)
    ax.set_facecolor(bg_color)
    if team_name == M.ateamName:
        ax.invert_xaxis()
        ax.invert_yaxis()

    z14 = hs = 0
    for _, row in df.iterrows():
        if 70 <= row['endX'] <= 88.54 and 22.66 <= row['endY'] <= 45.32:
            ax.add_patch(patches.FancyArrowPatch((row['x'], row['y']), (row['endX'], row['endY']),
                                                 arrowstyle='->', alpha=0.75, mutation_scale=20,
                                                 color='orange', linewidth=1.5))
            z14 += 1
        if row['endX'] >= 70 and 11.33 <= row['endY'] <= 22.66:
            ax.add_patch(patches.FancyArrowPatch((row['x'], row['y']), (row['endX'], row['endY']),
                                                 arrowstyle='->', alpha=0.75, mutation_scale=20,
                                                 color=col, linewidth=1.5))
            hs += 1
        if row['endX'] >= 70 and 45.32 <= row['endY'] <= 56.95:
            ax.add_patch(patches.FancyArrowPatch((row['x'], row['y']), (row['endX'], row['endY']),
                                                 arrowstyle='->', alpha=0.75, mutation_scale=20,
                                                 color=col, linewidth=1.5))
            hs += 1

    ax.fill([70, 88.54, 88.54, 70], [22.66, 22.66, 45.32, 45.32], 'orange', alpha=0.2, label='Zone14')
    ax.fill([70, 105, 105, 70], [11.33, 11.33, 22.66, 22.66], col, alpha=0.2, label='HalfSpaces')
    ax.fill([70, 105, 105, 70], [45.32, 45.32, 56.95, 56.95], col, alpha=0.2)

    ax.scatter(16.46, 13.85, color=col, s=15000, edgecolor=line_color, linewidth=2, alpha=1, marker='h')
    ax.scatter(16.46, 54.15, color='orange', s=15000, edgecolor=line_color, linewidth=2, alpha=1, marker='h')
    ax.text(16.46, 13.85 - 4, 'HalfSp', fontsize=20, color=line_color, ha='center', va='center', path_effects=path_eff)
    ax.text(16.46, 54.15 - 4, 'Zone14', fontsize=20, color=line_color, ha='center', va='center', path_effects=path_eff)
    ax.text(16.46, 13.85 + 2, f'{hs}', fontsize=40, color=line_color, ha='center', va='center', path_effects=path_eff)
    ax.text(16.46, 54.15 + 2, f'{z14}', fontsize=40, color=line_color, ha='center', va='center', path_effects=path_eff)

    if team_name == M.ateamName:
        ax.text(0, 73, '<--- Attacking Direction', color=M.acol, size=15, ha='right', va='center')
    else:
        ax.text(0, -5, 'Attacking Direction --->', color=M.hcol, size=15, ha='left', va='center')
    ax.set_title(f'{team_name} Zone14 & Halfsp. Pass', color=line_color, fontsize=30,
                 fontweight='bold', path_effects=path_eff)


# --------------------------------------------------------------------------
# 13 & 15 - Pass end zone
# --------------------------------------------------------------------------

def Pass_end_zone(ax, df, team_name, cm):
    pitch = Pitch(pitch_type='uefa', line_color=line_color, goal_type='box', goal_alpha=.5,
                  corner_arcs=True, line_zorder=2, pitch_color=bg_color, linewidth=2)
    pitch.draw(ax=ax)
    if team_name == M.ateamName:
        ax.invert_xaxis()
        ax.invert_yaxis()

    bin_statistic = pitch.bin_statistic_positional(df.endX, df.endY, statistic='count',
                                                   positional='full', normalize=True)
    pitch.heatmap_positional(bin_statistic, ax=ax, cmap=cm, edgecolors=bg_color)
    pitch.scatter(df.endX, df.endY, c='gray', s=5, ax=ax)
    pitch.label_heatmap(bin_statistic, color=line_color, fontsize=30, ax=ax, ha='center', va='center',
                        str_format='{:.0%}', path_effects=path_eff)

    if team_name == M.ateamName:
        ax.text(0, 73, '<--- Attacking Direction', color=M.acol, size=15, ha='right', va='center')
    else:
        ax.text(0, -5, 'Attacking Direction --->', color=M.hcol, size=15, ha='left', va='center')
    ax.set_title(f'{team_name} Pass End Zone', color=line_color, fontsize=30,
                 fontweight='bold', path_effects=path_eff)


# --------------------------------------------------------------------------
# 16 & 18 - Chance creating zone
# --------------------------------------------------------------------------

def Chance_creating_zone(ax, df, team_name, cm, tcm):
    pitch = Pitch(pitch_type='uefa', line_color=line_color, goal_type='box', goal_alpha=.5,
                  corner_arcs=True, line_zorder=2, pitch_color=bg_color, linewidth=2)
    pitch.draw(ax=ax)
    if team_name == M.ateamName:
        ax.invert_xaxis()
        ax.invert_yaxis()

    bin_statistic = pitch.bin_statistic_positional(df.x, df.y, statistic='count',
                                                   positional='full', normalize=False)
    pitch.heatmap_positional(bin_statistic, ax=ax, cmap=cm, edgecolors='gray')
    pitch.scatter(df.x, df.y, c='gray', s=5, ax=ax)

    cc = assists = 0
    for _, row in df.iterrows():
        is_assist = 'IntentionalGoalAssist' in row['qualifiers']
        ax.add_patch(patches.FancyArrowPatch((row['x'], row['y']), (row['endX'], row['endY']),
                                             arrowstyle='->', mutation_scale=20,
                                             color=green if is_assist else violet,
                                             linewidth=1.25, alpha=1, zorder=3))
        cc += 1
        assists += int(is_assist)

    pitch.label_heatmap(bin_statistic, color=line_color, fontsize=30, ax=ax, ha='center', va='center',
                        str_format='{:.0f}', path_effects=path_eff)

    if team_name == M.ateamName:
        ax.text(105, 71.5, 'violet arrow = key pass\ngreen arrow = assist', color=M.acol, size=15,
                ha='left', va='center')
        ax.text(0, 73, '<--- Attacking Direction', color=M.acol, size=15, ha='right', va='center')
        ax.text(52.5, -2, f'Total Chances Created = {cc}  (assists {assists})', color=tcm,
                fontsize=15, ha='center', va='center')
        ax.set_title(f'{M.ateamName} Chance Creating Zone', color=line_color, fontsize=30,
                     fontweight='bold', path_effects=path_eff)
    else:
        ax.text(105, -3.5, 'violet arrow = key pass\ngreen arrow = assist', color=M.hcol, size=15,
                ha='right', va='center')
        ax.text(0, -5, 'Attacking Direction --->', color=M.hcol, size=15, ha='left', va='center')
        ax.text(52.5, 70, f'Total Chances Created = {cc}  (assists {assists})', color=tcm,
                fontsize=15, ha='center', va='center')
        ax.set_title(f'{M.hteamName} Chance Creating Zone', color=line_color, fontsize=30,
                     fontweight='bold', path_effects=path_eff)


# --------------------------------------------------------------------------
# 2 - Shot map
# --------------------------------------------------------------------------

def plot_shotmap(ax):
    S, h, a, hcol, acol = M.Shotsdf, M.hteamID, M.ateamID, M.hcol, M.acol
    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.5, corner_arcs=True,
                  pitch_color=bg_color, linewidth=2, line_color=line_color)
    pitch.draw(ax=ax)

    def sel(team, stype, big):
        m = (S['teamId'] == team) & (S['type'] == stype)
        m = m & (S['qualifiers'].str.contains('BigChance') if big else ~S['qualifiers'].str.contains('BigChance'))
        return S[m]

    # home shots are mirrored so both teams attack towards the middle
    for big, base in ((False, 1.0), (True, 1.6)):
        hPost, hSave, hMiss, hGoal = (sel(h, t, big) for t in ('ShotOnPost', 'SavedShot', 'MissedShots', 'Goal'))
        pitch.scatter(105 - hPost.x, 68 - hPost.y, s=200 * base, edgecolors=hcol, c=hcol, marker='o', ax=ax)
        pitch.scatter(105 - hSave.x, 68 - hSave.y, s=200 * base, edgecolors=hcol, c='None', hatch='///////', marker='o', ax=ax)
        pitch.scatter(105 - hMiss.x, 68 - hMiss.y, s=200 * base, edgecolors=hcol, c='None', marker='o', ax=ax)
        pitch.scatter(105 - hGoal.x, 68 - hGoal.y, s=350 * base, edgecolors='green', linewidths=0.6,
                      c='None', marker='football', zorder=3, ax=ax)

        aPost, aSave, aMiss, aGoal = (sel(a, t, big) for t in ('ShotOnPost', 'SavedShot', 'MissedShots', 'Goal'))
        pitch.scatter(aPost.x, aPost.y, s=200 * base, edgecolors=acol, c=acol, marker='o', ax=ax)
        pitch.scatter(aSave.x, aSave.y, s=200 * base, edgecolors=acol, c='None', hatch='///////', marker='o', ax=ax)
        pitch.scatter(aMiss.x, aMiss.y, s=200 * base, edgecolors=acol, c='None', marker='o', ax=ax)
        pitch.scatter(aGoal.x, aGoal.y, s=350 * base, edgecolors='green', linewidths=0.6,
                      c='None', marker='football', zorder=3, ax=ax)

    pitch.scatter(105 - M.hogdf.x, 68 - M.hogdf.y, s=350, edgecolors='orange', linewidths=0.6,
                  c='None', marker='football', zorder=3, ax=ax)
    pitch.scatter(M.aogdf.x, M.aogdf.y, s=350, edgecolors='orange', linewidths=0.6,
                  c='None', marker='football', zorder=3, ax=ax)

    rows_n = 4 if not getattr(M, 'has_xg', False) else 7
    rows = [55 - i * 7 for i in range(rows_n)]
    if getattr(M, 'has_xg', False):
        home = [M.hgoal_count, M.hxg, M.hxgot, M.hTotalShots, M.hShotsOnT, M.hxGpSh, M.home_average_shot_distance]
        away = [M.agoal_count, M.axg, M.axgot, M.aTotalShots, M.aShotsOnT, M.axGpSh, M.away_average_shot_distance]
        labels = ['Goals', 'xG', 'xGOT', 'Shots', 'On Target', 'xG/Shot', 'Avg.Dist.']
    else:
        home = [M.hgoal_count, M.hTotalShots, M.hShotsOnT, M.home_average_shot_distance]
        away = [M.agoal_count, M.aTotalShots, M.aShotsOnT, M.away_average_shot_distance]
        labels = ['Goals', 'Shots', 'On Target', 'Avg.Dist.']

    def norm(x, y):
        return (x / (x + y) * 20) if (x + y) else 10.0

    hn = [norm(x, y) for x, y in zip(home, away)]
    an = [norm(y, x) for x, y in zip(home, away)]

    start_x = 42.5
    ax.barh(rows, hn, height=5, color=hcol, left=start_x)
    ax.barh(rows, an, height=5, left=[start_x + v for v in hn], color=acol)
    for s in ('top', 'right', 'bottom', 'left'):
        ax.spines[s].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    for y, lab, hv, av in zip(rows, labels, home, away):
        ax.text(52.5, y, lab, color=bg_color, fontsize=18, ha='center', va='center', fontweight='bold')
        ax.text(41.5, y, f'{hv}', color=line_color, fontsize=18, ha='right', va='center', fontweight='bold')
        ax.text(63.5, y, f'{av}', color=line_color, fontsize=18, ha='left', va='center', fontweight='bold')

    ax.text(0, 70, f'{M.hteamName} shots', color=hcol, size=25, ha='left', fontweight='bold', path_effects=path_eff)
    ax.text(105, 70, f'{M.ateamName} shots', color=acol, size=25, ha='right', fontweight='bold', path_effects=path_eff)
    ax.text(52.5, -4, 'filled = hit post   hatched = saved   hollow = off target   ball = goal   '
                      '(bigger marker = big chance)',
            color='gray', size=13, ha='center', va='center')


# --------------------------------------------------------------------------
# 5 - GK performance / goal mouth
# --------------------------------------------------------------------------

def plot_goalPost(ax):
    S, hcol, acol = M.Shotsdf, M.hcol, M.acol
    hShotsdf = S[S['teamId'] == M.hteamID].copy()
    aShotsdf = S[S['teamId'] == M.ateamID].copy()

    hShotsdf['goalMouthZ'] = hShotsdf['goalMouthZ'] * 0.75
    aShotsdf['goalMouthZ'] = aShotsdf['goalMouthZ'] * 0.75 + 38
    hShotsdf['goalMouthY'] = (37.66 - hShotsdf['goalMouthY']) * 12.295
    aShotsdf['goalMouthY'] = (37.66 - aShotsdf['goalMouthY']) * 12.295

    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.75, corner_arcs=True,
                  pitch_color=bg_color, line_color=bg_color, linewidth=2)
    pitch.draw(ax=ax)

    # away goal (bottom) then home goal (top)
    ax.plot([0, 0], [-1, 30], color=line_color, linewidth=5)
    ax.plot([0, 90], [30, 30], color=line_color, linewidth=5)
    ax.plot([90, 90], [30, -1], color=line_color, linewidth=5)
    ax.plot([-2, 92], [-2, -2], color=line_color, linewidth=3)
    ax.plot([0, 0], [37, 68], color=line_color, linewidth=5)
    ax.plot([0, 90], [68, 68], color=line_color, linewidth=5)
    ax.plot([90, 90], [68, 37], color=line_color, linewidth=5)
    ax.plot([-2, 92], [36, 36], color=line_color, linewidth=3)

    hSavedf = hShotsdf[(hShotsdf['type'] == 'SavedShot') & (~hShotsdf['qualifiers'].str.contains(': 82,'))]
    hGoaldf = hShotsdf[(hShotsdf['type'] == 'Goal') & (~hShotsdf['qualifiers'].str.contains('OwnGoal'))]
    hPostdf = hShotsdf[hShotsdf['type'] == 'ShotOnPost']
    aSavedf = aShotsdf[(aShotsdf['type'] == 'SavedShot') & (~aShotsdf['qualifiers'].str.contains(': 82,'))]
    aGoaldf = aShotsdf[(aShotsdf['type'] == 'Goal') & (~aShotsdf['qualifiers'].str.contains('OwnGoal'))]
    aPostdf = aShotsdf[aShotsdf['type'] == 'ShotOnPost']

    pitch.scatter(hSavedf.goalMouthY, hSavedf.goalMouthZ, marker='o', c='None', edgecolor=acol, hatch='/////', s=400, ax=ax)
    pitch.scatter(hGoaldf.goalMouthY, hGoaldf.goalMouthZ, marker='football', c='None', edgecolors='green', s=550, ax=ax)
    pitch.scatter(hPostdf.goalMouthY, hPostdf.goalMouthZ, marker='o', c='None', edgecolors='orange', hatch='/////', s=400, ax=ax)
    pitch.scatter(aSavedf.goalMouthY, aSavedf.goalMouthZ, marker='o', c='None', edgecolor=hcol, hatch='/////', s=400, ax=ax)
    pitch.scatter(aGoaldf.goalMouthY, aGoaldf.goalMouthZ, marker='football', c='None', edgecolors='green', s=550, ax=ax)
    pitch.scatter(aPostdf.goalMouthY, aPostdf.goalMouthZ, marker='o', c='None', edgecolors='orange', hatch='/////', s=400, ax=ax)

    hgk = gk_name(M.hteamID)
    agk = gk_name(M.ateamID)
    ax.text(0, 70, f'{M.ateamName} shots on {M.hteamName} goal  -  GK {hgk}', color=hcol,
            fontsize=22, ha='left', fontweight='bold')
    ax.text(0, -3, f'{M.hteamName} shots on {M.ateamName} goal  -  GK {agk}', color=acol,
            fontsize=22, ha='left', va='top', fontweight='bold')

    if getattr(M, 'has_xg', False):
        ax.text(92, 68, f'xGOT faced = {M.axgot}\nShots Saved = {len(aSavedf)}\nGoals Conceded = {len(aGoaldf)}'
                        f'\nGoals Prevented = {round(M.axgot - len(aGoaldf), 2)}',
                color=hcol, fontsize=15, va='top', ha='left')
        ax.text(92, 30, f'xGOT faced = {M.hxgot}\nShots Saved = {len(hSavedf)}\nGoals Conceded = {len(hGoaldf)}'
                        f'\nGoals Prevented = {round(M.hxgot - len(hGoaldf), 2)}',
                color=acol, fontsize=15, va='top', ha='left')
    else:
        ax.text(92, 68, f'Shots Saved = {len(aSavedf)}\nGoals Conceded = {len(aGoaldf)}',
                color=hcol, fontsize=15, va='top', ha='left')
        ax.text(92, 30, f'Shots Saved = {len(hSavedf)}\nGoals Conceded = {len(hGoaldf)}',
                color=acol, fontsize=15, va='top', ha='left')

    pitch.scatter(94, 48, marker='o', c='None', edgecolor=hcol, hatch='/////', s=150, ax=ax)
    pitch.scatter(94, 53, marker='football', c='None', edgecolors='green', s=150, ax=ax)
    pitch.scatter(94, 43, marker='o', c='None', edgecolors='orange', hatch='/////', s=150, ax=ax)
    pitch.scatter(94, 10, marker='o', c='None', edgecolor=acol, hatch='/////', s=150, ax=ax)
    pitch.scatter(94, 15, marker='football', c='None', edgecolors='green', s=150, ax=ax)
    pitch.scatter(94, 5, marker='o', c='None', edgecolors='orange', hatch='/////', s=150, ax=ax)

    for y, txt, c in ((53, 'Goal Conceded', 'green'), (48, 'Saved shot', hcol), (43, 'Shot on post', 'orange'),
                      (15, 'Goal Conceded', 'green'), (10, 'Saved shot', acol), (5, 'Shot on post', 'orange')):
        ax.text(96, y, txt, color=c, fontsize=15, va='center')

    ax.text(45, 33, 'Goalkeeper Performance  -  shots faced, seen from behind the goal',
            color='gray', fontsize=15, ha='center', va='center')


def gk_name(team_id):
    side = 'home' if team_id == M.hteamID else 'away'
    for p in M.mcd[side]['players']:
        if p.get('position') == 'GK' and p.get('isFirstEleven'):
            return get_short_name(p['name'])
    return ''


# --------------------------------------------------------------------------
# 8 - Match momentum
# --------------------------------------------------------------------------

def plot_Momentum(ax):
    mom = M.Momentumdf
    colors = [M.hcol if x > 0 else M.acol for x in mom['average_xT']]

    hgoal_list = M.homedf[(M.homedf['type'] == 'Goal') & (~M.homedf['qualifiers'].str.contains('OwnGoal'))]['minute'].tolist()
    agoal_list = M.awaydf[(M.awaydf['type'] == 'Goal') & (~M.awaydf['qualifiers'].str.contains('OwnGoal'))]['minute'].tolist()
    hog_list = M.homedf[(M.homedf['type'] == 'Goal') & (M.homedf['qualifiers'].str.contains('OwnGoal'))]['minute'].tolist()
    aog_list = M.awaydf[(M.awaydf['type'] == 'Goal') & (M.awaydf['qualifiers'].str.contains('OwnGoal'))]['minute'].tolist()

    highest_xT = mom['average_xT'].max()
    lowest_xT = mom['average_xT'].min()
    highest_minute = mom['minute'].max()

    ax.scatter(hgoal_list, [highest_xT] * len(hgoal_list), s=250, c='None', edgecolor='green', hatch='////', marker='o')
    ax.scatter(agoal_list, [lowest_xT] * len(agoal_list), s=250, c='None', edgecolor='green', hatch='////', marker='o')
    ax.scatter(hog_list, [lowest_xT] * len(hog_list), s=250, c='None', edgecolor='orange', hatch='////', marker='o')
    ax.scatter(aog_list, [highest_xT] * len(aog_list), s=250, c='None', edgecolor='orange', hatch='////', marker='o')

    for mn in hgoal_list + agoal_list + hog_list + aog_list:
        ax.axvline(mn, color='gray', linewidth=1, linestyle=':', alpha=0.6)

    ax.bar(mom['minute'], mom['average_xT'], color=colors)
    ax.axvline(45, color='gray', linewidth=2, linestyle='dashed')
    ax.set_facecolor(bg_color)
    for s in ('top', 'right', 'left', 'bottom'):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis='both', which='both', length=0)
    ax.tick_params(axis='x', colors=line_color, labelsize=14)
    ax.tick_params(axis='y', colors=line_color, labelsize=14)
    ax.set_xlabel('Minute', color=line_color, fontsize=20)
    ax.set_ylabel('Avg. xT per minute', color=line_color, fontsize=20)
    ax.axhline(y=0, color=line_color, alpha=1, linewidth=2)

    ax.set_xlim(-2, highest_minute * 1.24)
    ax.set_xticks(list(range(0, int(highest_minute) + 1, 15)))
    ax.text(highest_minute + 2, highest_xT, f'{M.hteamName}\nxT: {M.hxT}', color=M.hcol, fontsize=18, va='top', ha='left')
    ax.text(highest_minute + 2, lowest_xT, f'{M.ateamName}\nxT: {M.axT}', color=M.acol, fontsize=18, va='bottom', ha='left')
    ax.set_title('Match Momentum by xT', color=line_color, fontsize=30, fontweight='bold')


# --------------------------------------------------------------------------
# 11 - Match stats
# --------------------------------------------------------------------------

def plotting_match_stats(ax):
    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.75, corner_arcs=True,
                  pitch_color=bg_color, line_color=bg_color, linewidth=2)
    pitch.draw(ax=ax)

    ax.fill([0, 0, 105, 105], [62, 68, 68, 62], 'orange')
    ax.text(52.5, 64.5, 'Match Stats', ha='center', va='center', color=line_color, fontsize=25,
            fontweight='bold', path_effects=path_eff)

    rows = [58 - i * 6 for i in range(11)]
    stats_home = [M.hposs, M.hft, M.htotalPass, M.hLongB, M.hCor, M.hglkl, M.htkl, M.hintc, M.hclr, M.harl, M.hblrc]
    stats_away = [M.aposs, M.aft, M.atotalPass, M.aLongB, M.aCor, M.aglkl, M.atkl, M.aintc, M.aclr, M.aarl, M.ablrc]

    def frac(x, y):
        return (x / (x + y) * 45) if (x + y) else 22.5

    hn = [-frac(x, y) for x, y in zip(stats_home, stats_away)]
    an = [frac(y, x) for x, y in zip(stats_home, stats_away)]

    ax.barh(rows, hn, height=4, color=M.hcol, left=52.5)
    ax.barh(rows, an, height=4, left=52.5, color=M.acol)
    for s in ('top', 'right', 'bottom', 'left'):
        ax.spines[s].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])

    labels = ['Possession', 'Field Tilt', 'Passes (Acc.)', 'LongBalls (Acc.)', 'Corners', 'Goalkick len.',
              'Tackles (Wins)', 'Interceptions', 'Clearance', 'Aerials (Wins)', 'Ball Recoveries']
    hvals = [f'{int(M.hposs)}%', f'{int(M.hft)}%', f'{M.htotalPass}({M.hAccPass})', f'{M.hLongB}({M.hAccLongB})',
             f'{M.hCor}', f'{M.hglkl}', f'{M.htkl}({M.htklw})', f'{M.hintc}', f'{M.hclr}',
             f'{M.harl}({M.harlw})', f'{M.hblrc}']
    avals = [f'{int(M.aposs)}%', f'{int(M.aft)}%', f'{M.atotalPass}({M.aAccPass})', f'{M.aLongB}({M.aAccLongB})',
             f'{M.aCor}', f'{M.aglkl}', f'{M.atkl}({M.atklw})', f'{M.aintc}', f'{M.aclr}',
             f'{M.aarl}({M.aarlw})', f'{M.ablrc}']

    for y, lab, hv, av in zip(rows, labels, hvals, avals):
        ax.text(52.5, y, lab, color=line_color, fontsize=17, ha='center', va='center',
                fontweight='bold', path_effects=path_eff)
        ax.text(7.5, y, hv, color=line_color, fontsize=20, ha='right', va='center', fontweight='bold')
        ax.text(97.5, y, av, color=line_color, fontsize=20, ha='left', va='center', fontweight='bold')

    ax.text(7.5, 71, M.hteamName, color=M.hcol, fontsize=22, ha='right', va='center', fontweight='bold')
    ax.text(97.5, 71, M.ateamName, color=M.acol, fontsize=22, ha='left', va='center', fontweight='bold')


# --------------------------------------------------------------------------
# 14 - High turnovers
# --------------------------------------------------------------------------

def HighTO(ax):
    df = M.df
    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.75, corner_arcs=True,
                  pitch_color=bg_color, line_color=line_color, linewidth=2)
    pitch.draw(ax=ax)
    ax.set_ylim(-0.5, 68.5)
    ax.set_xlim(-0.5, 105.5)

    home_TO = df[(df['teamId'] == M.hteamID) & (df['type'].isin(['BallRecovery', 'Interception'])) & (df['x'] >= 70)].copy()
    away_TO = df[(df['teamId'] == M.ateamID) & (df['type'].isin(['BallRecovery', 'Interception'])) & (df['x'] >= 70)].copy()
    home_TO = home_TO[((home_TO['x'] - 105) ** 2 + (home_TO['y'] - 34) ** 2) ** 0.5 <= 40]
    away_TO = away_TO[((away_TO['x'] - 105) ** 2 + (away_TO['y'] - 34) ** 2) ** 0.5 <= 40]

    ax.scatter(105 - home_TO.x, 68 - home_TO.y, s=250, c=M.hcol, edgecolor=line_color, marker='o', linewidth=2, zorder=3)
    ax.scatter(away_TO.x, away_TO.y, s=250, c=M.acol, edgecolor=line_color, marker='o', linewidth=2, zorder=3)

    ax.add_artist(plt.Circle((0, 34), 40, color=M.hcol, fill=True, alpha=0.25, linestyle='dashed'))
    ax.add_artist(plt.Circle((105, 34), 40, color=M.acol, fill=True, alpha=0.25, linestyle='dashed'))
    ax.set_aspect('equal', adjustable='box')

    ax.text(0, 70, f'{M.hteamName}\nHigh Turnover: {len(home_TO)}', color=M.hcol, size=22, ha='left', fontweight='bold')
    ax.text(105, 70, f'{M.ateamName}\nHigh Turnover: {len(away_TO)}', color=M.acol, size=22, ha='right', fontweight='bold')
    ax.text(52.5, -4, 'possession won within 40m of the opponent goal', color='gray', size=13,
            ha='center', va='center')


# --------------------------------------------------------------------------
# 17 - Crosses
# --------------------------------------------------------------------------

def Crosses(ax):
    df = M.df
    pitch = Pitch(pitch_type='uefa', goal_type='box', goal_alpha=.75, corner_arcs=True,
                  pitch_color=bg_color, line_color=line_color, linewidth=2)
    pitch.draw(ax=ax)
    ax.set_ylim(-0.5, 68.5)
    ax.set_xlim(-0.5, 105.5)

    q = df['qualifiers']
    home_cross = df[(df['teamId'] == M.hteamID) & (df['type'] == 'Pass') & q.str.contains('Cross') & ~q.str.contains('Corner')]
    away_cross = df[(df['teamId'] == M.ateamID) & (df['type'] == 'Pass') & q.str.contains('Cross') & ~q.str.contains('Corner')]

    hsuc = hunsuc = asuc = aunsuc = 0
    for _, row in home_cross.iterrows():
        ok = row['outcomeType'] == 'Successful'
        ax.add_patch(patches.FancyArrowPatch((105 - row['x'], 68 - row['y']), (105 - row['endX'], 68 - row['endY']),
                                             arrowstyle='->', mutation_scale=15 if ok else 10,
                                             color='green' if ok else 'red', linewidth=1.5, alpha=1 if ok else .65))
        hsuc, hunsuc = hsuc + int(ok), hunsuc + int(not ok)
    for _, row in away_cross.iterrows():
        ok = row['outcomeType'] == 'Successful'
        ax.add_patch(patches.FancyArrowPatch((row['x'], row['y']), (row['endX'], row['endY']),
                                             arrowstyle='->', mutation_scale=15 if ok else 10,
                                             color='green' if ok else 'red', linewidth=1.5, alpha=1 if ok else .65))
        asuc, aunsuc = asuc + int(ok), aunsuc + int(not ok)

    home_left = len(home_cross[home_cross['y'] >= 34])
    home_right = len(home_cross[home_cross['y'] < 34])
    away_left = len(away_cross[away_cross['y'] >= 34])
    away_right = len(away_cross[away_cross['y'] < 34])

    ax.text(51, 2, f'Crosses from\nLeftwing: {home_left}', color=M.hcol, fontsize=15, va='bottom', ha='right')
    ax.text(51, 66, f'Crosses from\nRightwing: {home_right}', color=M.hcol, fontsize=15, va='top', ha='right')
    ax.text(54, 66, f'Crosses from\nLeftwing: {away_left}', color=M.acol, fontsize=15, va='top', ha='left')
    ax.text(54, 2, f'Crosses from\nRightwing: {away_right}', color=M.acol, fontsize=15, va='bottom', ha='left')

    ax.text(0, -2, f'Successful: {hsuc}', color='green', fontsize=18, ha='left', va='top')
    ax.text(0, -6, f'Unsuccessful: {hunsuc}', color='red', fontsize=18, ha='left', va='top')
    ax.text(105, -2, f'Successful: {asuc}', color='green', fontsize=18, ha='right', va='top')
    ax.text(105, -6, f'Unsuccessful: {aunsuc}', color='red', fontsize=18, ha='right', va='top')

    ax.text(0, 70, f'{M.hteamName} Crosses', color=M.hcol, size=22, ha='left', fontweight='bold')
    ax.text(105, 70, f'{M.ateamName} Crosses', color=M.acol, size=22, ha='right', fontweight='bold')


# --------------------------------------------------------------------------
# Header with both club crests
# --------------------------------------------------------------------------

CREDIT = 'Data: WhoScored / Opta  |  xT grid: mckayjohns  |  Built with mplsoccer'
AUTHOR = '@maf74__'
AUTHOR_LINK = 'linkedin.com/in/mohamed-atef-fahmy-khalil'


def draw_footer(fig, y=0.012, size=11):
    """Signature bottom-left (handle in bold), data credits bottom-right."""
    try:
        from highlight_text import fig_text
        fig_text(0.012, y, f'<{AUTHOR}>   ·   {AUTHOR_LINK}', fig=fig,
                 color=faint_color, fontsize=size, ha='left', va='center',
                 highlight_textprops=[{'color': line_color, 'fontweight': 'bold',
                                       'fontsize': size * 1.25}])
    except Exception:                                            # noqa: BLE001
        fig.text(0.012, y, f'{AUTHOR}   ·   {AUTHOR_LINK}', color=line_color,
                 fontsize=size, ha='left', va='center')
    fig.text(0.988, y, CREDIT, color=credit_color, fontsize=size, ha='right', va='center')


LOGO_FILES = {
    'Arsenal': 'arsenal.png',
    'Crystal Palace': 'crystal_palace.png',
}


@lru_cache(maxsize=1)
def _logo_index():
    """canonical club key -> crest filename, from assets/logos.json.

    The filenames were derived from the team name before this
    ('Newcastle United' -> newcastle_united.png), which meant WhoScored's
    short names missed almost every crest: it calls them 'Newcastle', so the
    lookup asked for newcastle.png, found nothing, and drew no crest at all.
    Keying on club_key makes either feed's spelling land on the same file.
    """
    from scraper.sofascore import club_key

    index = {}
    try:
        with open(os.path.join(ASSETS, 'logos.json')) as fh:
            catalogue = json.load(fh)
    except (OSError, ValueError):
        catalogue = {}

    for name, entry in catalogue.items():
        for alias in (name, entry.get('verified_name')):
            key = club_key(alias) if alias else None
            if key:
                index.setdefault(key, entry['file'])
    return index


def load_logo(team_name):
    """The club's crest, or None when we do not ship one.

    A missing crest is not an error - the header simply leaves the space empty.
    Burnley, West Ham and Wolves have no asset yet.
    """
    from scraper.sofascore import club_key

    fname = LOGO_FILES.get(team_name)
    if not fname:
        fname = _logo_index().get(club_key(team_name) or '')
    if not fname:
        fname = team_name.lower().replace(' ', '_') + '.png'

    path = os.path.join(ASSETS, fname)
    if not os.path.exists(path):
        return None
    try:
        return mpimg.imread(path)
    except Exception as exc:                                          # noqa: BLE001
        # imread picks its decoder from the extension, so an asset that is not
        # the format its name claims raises here. Five crests were WebP named
        # .png, and one of them - Chelsea - failed a whole match at render
        # after the scrape, transform and enrichment had all succeeded.
        logger.warning('crest %s could not be read (%s); rendering without it',
                       fname, exc)
        return None


def draw_header(fig, spec, big=True):
    """Header band: home crest | scoreline + match context | away crest.

    Each crest gets its own axes so it keeps its aspect ratio and can never
    overlap the text column.
    """
    from matplotlib.gridspec import GridSpecFromSubplotSpec
    inner = GridSpecFromSubplotSpec(1, 3, subplot_spec=spec, width_ratios=[1, 5.2, 1], wspace=0.02)

    for i, team in ((0, M.hteamName), (2, M.ateamName)):
        ax = fig.add_subplot(inner[0, i])
        ax.axis('off')
        logo = load_logo(team)
        if logo is not None:
            ax.imshow(logo)

    ax = fig.add_subplot(inner[0, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')

    sc = 1.0 if big else 0.62
    ax.text(50, 78, f'{M.hteamName}  {M.hgoal_count} - {M.agoal_count}  {M.ateamName}',
            color=line_color, fontsize=48 * sc, fontweight='bold', ha='center', va='center')

    ht = (M.mcd.get('htScore') or '').replace(' ', '')
    sub = M.league or ''
    if M.gw:
        sub += f" | {'MD' if 'Champions' in (M.league or '') else 'GW'} {M.gw}"
    sub += f' | {M.date} | {M.venue}'
    if ht:
        sub += f' | HT {ht}'
    ax.text(50, 47, sub, color=muted_color, fontsize=23 * sc, ha='center', va='center')

    header_bits = [
        f'xT  {M.hxT} - {M.axT}',
        f'Possession  {int(M.hposs)}% - {int(M.aposs)}%',
        f'Shots  {M.hTotalShots} - {M.aTotalShots}',
    ]
    if getattr(M, 'has_xg', False):
        header_bits.append(f'xG  {M.hxg} - {M.axg}')
    ax.text(50, 22, '     '.join(header_bits),
            color=muted_color, fontsize=20 * sc, ha='center', va='center')

    ax.plot([0, 100], [2, 2], color=accent_color, linewidth=2)
    ax.plot([0, 100], [98, 98], color=accent_color, linewidth=2)


COMPETITION_LOGOS = {'Champions League': 'ucl.png'}


def load_competition_logo(league):
    """The competition's logo for the header, or None when we do not ship one."""
    fname = next((f for k, f in COMPETITION_LOGOS.items() if k in (league or '')), None)
    path = os.path.join(ASSETS, fname) if fname else None
    if not path or not os.path.exists(path):
        return None
    return mpimg.imread(path)


def draw_competition_watermark(ax, alpha=0.07):
    """Faint competition logo centred in a panel.

    Drawn above the panel content at low alpha rather than underneath it:
    heatmaps and zone fills are opaque and would hide a logo placed below them.
    """
    logo = load_competition_logo(M.league)
    if logo is None:
        return
    wax = ax.inset_axes([0.25, 0.1, 0.5, 0.8], zorder=50)
    wax.imshow(logo, alpha=alpha)
    wax.axis('off')


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

PANELS = [
    ('01_home_pass_network', lambda ax: pass_network_visualization(
        ax, M.home_passes_between_df, M.home_average_locs_and_count_df, M.hcol, M.home_team_id)),
    ('02_shot_map', plot_shotmap),
    ('03_away_pass_network', lambda ax: pass_network_visualization(
        ax, M.away_passes_between_df, M.away_average_locs_and_count_df, M.acol, M.away_team_id)),
    ('04_home_defensive_block', lambda ax: defensive_block(
        ax, M.defensive_home_average_locs_and_count_df, M.hteamID, M.hcol)),
    ('05_gk_performance', plot_goalPost),
    ('06_away_defensive_block', lambda ax: defensive_block(
        ax, M.defensive_away_average_locs_and_count_df, M.ateamID, M.acol)),
    ('07_home_progressive_passes', lambda ax: draw_progressive_pass_map(ax, M.dfhpp, M.hteamName, M.hcol)),
    ('08_match_momentum', plot_Momentum),
    ('09_away_progressive_passes', lambda ax: draw_progressive_pass_map(ax, M.dfapp, M.ateamName, M.acol)),
    ('10_home_danger_zones', lambda ax: draw_pass_map(ax, M.dfhp, M.hteamName, M.hcol)),
    ('11_match_stats', plotting_match_stats),
    ('12_away_danger_zones', lambda ax: draw_pass_map(ax, M.dfap, M.ateamName, M.acol)),
    ('13_home_pass_end_zones', lambda ax: Pass_end_zone(ax, M.hdfPEZ, M.hteamName, M.pearl_cmaph)),
    ('14_high_turnovers', HighTO),
    ('15_away_pass_end_zones', lambda ax: Pass_end_zone(ax, M.adfPEZ, M.ateamName, M.pearl_cmapa)),
    ('16_home_chance_creating', lambda ax: Chance_creating_zone(ax, M.dfchch, M.hteamName, M.pearl_cmaph, M.hcol)),
    ('17_crosses_analysis', Crosses),
    ('18_away_chance_creating', lambda ax: Chance_creating_zone(ax, M.dfchca, M.ateamName, M.pearl_cmapa, M.acol)),
]


# Upload-ready JPEGs live apart from the archive PNGs, so panels/ holds one
# file per panel and linkedin/ is exactly what gets attached to a post.
SOCIAL_DIR = 'linkedin'


def render_individual(out_dir, dpi=150):
    written = []
    for name, fn in PANELS:
        fig = plt.figure(figsize=(14, 12), facecolor=bg_color)
        gs = GridSpec(2, 1, height_ratios=[1, 5.4], hspace=0.20, figure=fig)
        draw_header(fig, gs[0], big=False)
        ax = fig.add_subplot(gs[1])
        ax.set_facecolor(bg_color)
        fn(ax)
        draw_competition_watermark(ax)
        draw_footer(fig, y=0.012, size=11)
        path = os.path.join(out_dir, f'{name}.png')
        fig.savefig(path, bbox_inches='tight', facecolor=bg_color, dpi=dpi)
        plt.close(fig)
        make_png_upload_safe(path, bg=bg_color)
        social = export_social_jpg(path, bg=bg_color, unified=False, suffix='',
                                   out_dir=os.path.join(os.path.dirname(out_dir), SOCIAL_DIR))
        written.append(path)
        print(f'  wrote {os.path.basename(path)}')
        print(f'    + {os.path.basename(social)} ({social.stat().st_size // 1024} KB)')
    return written


def render_dashboard(out_dir, dpi=100):
    fig = plt.figure(figsize=(40, 62), facecolor=bg_color)
    gs = GridSpec(7, 3, height_ratios=[0.9] + [1] * 6, hspace=0.26, wspace=0.10, figure=fig)

    draw_header(fig, gs[0, :], big=True)

    axes = [[fig.add_subplot(gs[r + 1, c]) for c in range(3)] for r in range(6)]
    for row in axes:
        for ax in row:
            ax.set_facecolor(bg_color)

    for (name, fn), ax in zip(PANELS, [ax for row in axes for ax in row]):
        fn(ax)
        draw_competition_watermark(ax)

    draw_footer(fig, y=0.085, size=22)
    path = os.path.join(out_dir, '00_match_dashboard_unified.png')
    fig.savefig(path, bbox_inches='tight', facecolor=bg_color, dpi=dpi)
    plt.close(fig)
    # matplotlib emits RGBA even here, where the figure is fully opaque, and an
    # alpha channel is enough on its own to fail a LinkedIn or X upload.
    make_png_upload_safe(path, bg=bg_color)
    social = export_social_jpg(path, bg=bg_color, unified=True, suffix='',
                               out_dir=os.path.join(out_dir, SOCIAL_DIR))
    print(f'  wrote {os.path.basename(path)}')
    print(f'    + {os.path.basename(social)} ({social.stat().st_size // 1024} KB)')
    return path


def render_match(job: str, output_root: str | None = None, season: str | None = None,
                 matchweek: int | None = None, dpi: int = 150, only: str | None = None,
                 club: str | None = None, theme: str = 'classic') -> dict:
    """Render into the season layout: output/[<club>/]<season>/mw<NN>_<date>_<h>_v_<a>/.

    `club` nests one level so each club keeps its own index; omit it for Arsenal,
    whose output predates the club level.
    """
    from visualizer import output_layout

    apply_theme(theme)
    prepare(job)
    season = season or M.meta.get('season')
    matchweek = matchweek if matchweek is not None else (M.gw or None)

    match_path = output_layout.match_dir(
        season=season, date=M.date, home=M.hteamName, away=M.ateamName,
        matchweek=matchweek, competition=M.league, output_root=output_root,
        club=club)
    panels_path = output_layout.panels_dir(match_path)

    if only != 'dashboard':
        render_individual(panels_path, dpi=dpi)
    if only != 'panels':
        render_dashboard(match_path)

    result = output_layout.finalise(match_path, {
        'job_id': job, 'source': 'whoscored', 'club': club,
        'match_id': M.data.get('matchId') or M.mcd.get('matchId'),
        'season': season, 'matchweek': matchweek, 'competition': M.league,
        'date': M.date, 'venue': M.venue,
        'home_team': M.hteamName, 'away_team': M.ateamName,
        'score': f'{M.hgoal_count}-{M.agoal_count}',
        'events': int(len(M.df)),
        'stats': {'xt': [M.hxT, M.axT], 'possession': [M.hposs, M.aposs],
                  'shots': [M.hTotalShots, M.aTotalShots], 'xg': [M.hxg, M.axg]},
    })
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', default=None, help='processed job id (default: newest scrape)')
    ap.add_argument('--out', default=None,
                    help='flat output dir (legacy); omit to use the season layout')
    ap.add_argument('--output-root', default=None, help='root of the season layout')
    ap.add_argument('--season', default=None)
    ap.add_argument('--matchweek', type=int, default=None)
    ap.add_argument('--dpi', type=int, default=150)
    ap.add_argument('--only', choices=['panels', 'dashboard'], default=None)
    ap.add_argument('--club', default=None,
                    help='nest the output under output/<club>/ (default: flat)')
    ap.add_argument('--theme', choices=sorted(THEMES), default='classic')
    args = ap.parse_args()
    apply_theme(args.theme)

    job = args.job or latest_job()

    if args.out:                      # legacy flat layout
        os.makedirs(args.out, exist_ok=True)
        prepare(job)
        _print_summary(job, args.out)
        if args.only != 'dashboard':
            render_individual(args.out, dpi=args.dpi)
        if args.only != 'panels':
            render_dashboard(args.out)
    else:
        result = render_match(job, output_root=args.output_root, season=args.season,
                              matchweek=args.matchweek, dpi=args.dpi, only=args.only,
                              club=args.club, theme=args.theme)
        _print_summary(job, result['match_dir'])
    print('Done.')


def _print_summary(job: str, out: str) -> None:
    print(f'Match : {M.hteamName} {M.hgoal_count}-{M.agoal_count} {M.ateamName}')
    print(f'Job   : {job}   events: {len(M.df)}   date: {M.date}   venue: {M.venue}')
    print(f'xT    : {M.hxT} - {M.axT} | possession {M.hposs}% - {M.aposs}% '
          f'| shots {M.hTotalShots} - {M.aTotalShots}')
    print(f'Output: {out}')


if __name__ == '__main__':
    main()
