"""Derive warehouse-ready aggregates from a prepared match.

Everything here is computed from the *same* `M` namespace that renders the 18
dashboard panels (`visualizer.match_report.prepare`), so the numbers stored in
Postgres are by construction the numbers on the PNGs.
"""

import ast

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_qualifiers(value):
    """Turn the panels' qualifier string back into real structured data.

    `visualizer.match_report` stringifies each event's qualifier list with
    `str()` so that the notebook's `qualifiers.str.contains(...)` filters keep
    working - a Python repr, single quotes and all. That form is load-bearing
    for the panels and stays.

    It must not reach Postgres, though. `events.qualifiers` is declared JSONB
    with a GIN index, and writing the repr stored a JSON *string* instead of an
    array: `jsonb_typeof` returned 'string', the index was unusable, and every
    set-piece, pass-type or pressing feature had to be matched with LIKE at
    query time. So the repr is parsed back here, at the boundary between the
    render path and the warehouse.

    `ast.literal_eval` rather than `json.loads` because the input is Python
    syntax, not JSON. Anything unparseable is dropped to NULL rather than
    stored as a lie about its own type.
    """
    if value is None or isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in ('nan', 'None'):
        return None
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    return parsed if isinstance(parsed, (list, dict)) else None

def _sum_stat(stats: dict, key: str) -> float:
    """WhoScored player stats are {stat: {minute: value}} - sum the values."""
    block = (stats or {}).get(key) or {}
    try:
        return float(sum(block.values()))
    except TypeError:
        return 0.0


def _last_stat(stats: dict, key: str):
    """Take the value at the highest minute (used for ratings)."""
    block = (stats or {}).get(key) or {}
    if not block:
        return None
    last_minute = max(block, key=lambda k: int(k))
    return float(block[last_minute])


def _n(df: pd.DataFrame, cond) -> int:
    return int(cond.sum()) if len(df) else 0


# ---------------------------------------------------------------------------
# match / team level
# ---------------------------------------------------------------------------

def _xg_written(M) -> bool:
    """Did the enrichment actually write xG, or are these default zeroes?

    `M.hxg` and friends default to 0.0 when metadata carries no xG, so the
    numbers alone cannot tell "no data" from "genuinely nothing". `xg_source`
    can: enrich_xg sets it to 'sofascore' only when it wrote real values, and
    to 'unavailable: ...' when it did not.

    This matters because 0.0 is an ordinary xGOT - a side with no shots on
    target has exactly that, and three of Arsenal's first ten 2025/26 matches
    did. Treating it as missing loses real data.
    """
    return (M.meta.get('xg_source') or '').startswith(('sofascore', 'fotmob', 'manual'))


def _xg(value, written: bool):
    return value if written else None


def match_row(M) -> dict:
    mcd = M.mcd
    kickoff = mcd.get('startTime') or mcd.get('startDate')
    ht = (mcd.get('htScore') or '').split(':')
    has_xg = _xg_written(M)
    return {
        'match_id': int(mcd.get('matchId') or M.data.get('matchId')),
        'competition': M.league or 'Premier League',
        'matchweek': M.gw or None,
        'kickoff_utc': kickoff,
        'match_date': M.date,
        'home_team_id': int(M.hteamID),
        'away_team_id': int(M.ateamID),
        'home_score': int(M.hgoal_count),
        'away_score': int(M.agoal_count),
        'ht_home_score': int(ht[0]) if len(ht) == 2 and ht[0].strip().isdigit() else None,
        'ht_away_score': int(ht[1]) if len(ht) == 2 and ht[1].strip().isdigit() else None,
        'home_xg': _xg(M.hxg, has_xg),
        'away_xg': _xg(M.axg, has_xg),
        'home_xgot': _xg(M.hxgot, has_xg),
        'away_xgot': _xg(M.axgot, has_xg),
        'xg_source': M.meta.get('xg_source') if has_xg else None,
        'venue_name': M.venue or None,
        'referee': M.referee or None,
        'attendance': M.attendance,
        'max_minute': mcd.get('maxMinute'),
        'event_count': int(len(M.df)),
        'job_id': M.job_id,
    }


def team_rows(M) -> list[dict]:
    """Two rows - one per team - mirroring the Match Stats + Shot Map panels."""
    df = M.df
    has_xg = _xg_written(M)
    rows = []
    for side in ('home', 'away'):
        home = side == 'home'
        tid = M.hteamID if home else M.ateamID
        oid = M.ateamID if home else M.hteamID
        p = (lambda h, a: h if home else a)

        team = df[df['teamId'] == tid]
        q = team['qualifiers']
        is_pass = team['type'] == 'Pass'
        succ = team['outcomeType'] == 'Successful'

        prog = _n(team, is_pass & (team['pro'] >= 9.144) & ~q.str.contains('Corner'))
        key_passes = _n(team, q.str.contains('KeyPass', na=False))

        # zone 14 / half space, matching draw_pass_map's rules
        openp = team[is_pass & succ & ~q.str.contains('Corner')]
        z14 = _n(openp, (openp['endX'].between(70, 88.54)) & (openp['endY'].between(22.66, 45.32)))
        hs = _n(openp, (openp['endX'] >= 70) & (openp['endY'].between(11.33, 22.66))) + \
             _n(openp, (openp['endX'] >= 70) & (openp['endY'].between(45.32, 56.95)))

        to = team[team['type'].isin(['BallRecovery', 'Interception']) & (team['x'] >= 70)]
        high_to = _n(to, (((to['x'] - 105) ** 2 + (to['y'] - 34) ** 2) ** 0.5) <= 40)

        shots = M.hTotalShots if home else M.aTotalShots
        big = _n(team, team['type'].isin(['Goal', 'SavedShot', 'MissedShots', 'ShotOnPost'])
                 & q.str.contains('BigChance'))

        da = M.defensive_home_average_locs_and_count_df if home else M.defensive_away_average_locs_and_count_df
        net = M.home_average_locs_and_count_df if home else M.away_average_locs_and_count_df

        rows.append({
            'team_id': int(tid),
            'is_home': home,
            'opponent_team_id': int(oid),
            'goals': p(M.hgoal_count, M.agoal_count),
            'goals_conceded': p(M.agoal_count, M.hgoal_count),
            'xg': _xg(p(M.hxg, M.axg), has_xg),
            'xgot': _xg(p(M.hxgot, M.axgot), has_xg),
            'xt': p(M.hxT, M.axT),
            'possession_pct': p(M.hposs, M.aposs),
            'field_tilt_pct': p(M.hft, M.aft),
            'passes': p(M.htotalPass, M.atotalPass),
            'passes_accurate': p(M.hAccPass, M.aAccPass),
            'long_balls': p(M.hLongB, M.aLongB),
            'long_balls_accurate': p(M.hAccLongB, M.aAccLongB),
            'crosses': p(M.hCrss, M.aCrss),
            'crosses_accurate': p(M.hAccCrss, M.aAccCrss),
            'corners': p(M.hCor, M.aCor),
            'throw_ins': p(M.htins, M.atins),
            'goalkick_avg_length': p(M.hglkl, M.aglkl) or None,
            'progressive_passes': prog,
            'key_passes': key_passes,
            'zone14_passes': z14,
            'halfspace_passes': hs,
            'take_ons': p(M.htotalDrb, M.atotalDrb),
            'take_ons_won': p(M.hAccDrb, M.aAccDrb),
            'shots': shots,
            'shots_on_target': p(M.hShotsOnT, M.aShotsOnT),
            'big_chances': big,
            'avg_shot_distance': p(M.home_average_shot_distance, M.away_average_shot_distance),
            'tackles': p(M.htkl, M.atkl),
            'tackles_won': p(M.htklw, M.atklw),
            'interceptions': p(M.hintc, M.aintc),
            'clearances': p(M.hclr, M.aclr),
            'aerials': p(M.harl, M.aarl),
            'aerials_won': p(M.harlw, M.aarlw),
            'ball_recoveries': p(M.hblrc, M.ablrc),
            'blocked_passes': p(M.hblkp, M.ablkp),
            'fouls': p(M.hfoul, M.afoul),
            'offsides': p(M.hofs, M.aofs),
            'high_turnovers': high_to,
            'def_action_height_m': round(float(da['x'].mean()) * 1.05, 2) if len(da) else None,
            'pass_line_height_m': round(float(net['pass_avg_x'].median()) * 1.05, 2) if len(net) else None,
        })
    return rows


# ---------------------------------------------------------------------------
# player level
# ---------------------------------------------------------------------------

def player_rows(M) -> list[dict]:
    df = M.df
    max_minute = M.mcd.get('maxMinute') or 90
    rows = []

    for side in ('home', 'away'):
        team_id = M.hteamID if side == 'home' else M.ateamID
        for pl in M.mcd[side]['players']:
            pid = int(pl['playerId'])
            stats = pl.get('stats') or {}
            ev = df[df['playerId'] == pid]
            q = ev['qualifiers'] if len(ev) else pd.Series(dtype=str)

            starter = bool(pl.get('isFirstEleven'))
            on = pl.get('subbedInExpandedMinute')
            off = pl.get('subbedOutExpandedMinute')
            if starter:
                minutes = (off if off is not None else max_minute)
            elif on is not None:
                minutes = (off if off is not None else max_minute) - on
            else:
                minutes = 0

            goals = _n(ev, (ev['type'] == 'Goal') & ~q.str.contains('OwnGoal')) if len(ev) else 0
            own_goals = _n(ev, (ev['type'] == 'Goal') & q.str.contains('OwnGoal')) if len(ev) else 0
            assists = _n(ev, q.str.contains('IntentionalGoalAssist', na=False)) if len(ev) else 0
            is_pass = (ev['type'] == 'Pass') if len(ev) else pd.Series(dtype=bool)
            succ = (ev['outcomeType'] == 'Successful') if len(ev) else pd.Series(dtype=bool)

            cards = ev[ev['type'] == 'Card'] if len(ev) else ev
            yellows = _n(cards, cards['cardType'].astype(str).str.contains('Yellow', na=False)) if len(cards) else 0
            reds = _n(cards, cards['cardType'].astype(str).str.contains('Red', na=False)) if len(cards) else 0

            rows.append({
                'player_id': pid,
                'team_id': int(team_id),
                'name': pl.get('name'),
                'height_cm': pl.get('height'),
                'weight_kg': pl.get('weight'),
                'age': pl.get('age'),
                'shirt_no': pl.get('shirtNo'),
                'position': pl.get('position'),
                'is_first_eleven': starter,
                'is_motm': bool(pl.get('isManOfTheMatch')),
                'minutes_played': int(max(minutes, 0)),
                'subbed_on_minute': on,
                'subbed_off_minute': off,
                'rating': _last_stat(stats, 'ratings'),
                'touches': int(_sum_stat(stats, 'touches')),
                'goals': goals,
                'own_goals': own_goals,
                'assists': assists,
                'shots': _n(ev, ev['type'].isin(['Goal', 'SavedShot', 'MissedShots', 'ShotOnPost'])) if len(ev) else 0,
                'shots_on_target': int(_sum_stat(stats, 'shotsOnTarget')),
                'key_passes': _n(ev, q.str.contains('KeyPass', na=False)) if len(ev) else 0,
                'passes': _n(ev, is_pass) if len(ev) else 0,
                'passes_accurate': _n(ev, is_pass & succ) if len(ev) else 0,
                'progressive_passes': _n(ev, is_pass & (ev['pro'] >= 9.144) & ~q.str.contains('Corner')) if len(ev) else 0,
                'through_balls': _n(ev, is_pass & q.str.contains('Throughball', na=False)) if len(ev) else 0,
                'crosses': _n(ev, is_pass & q.str.contains('Cross', na=False)) if len(ev) else 0,
                'xt': round(float(ev['xT'].sum()), 3) if len(ev) else 0.0,
                'take_ons': int(_sum_stat(stats, 'dribblesAttempted')),
                'take_ons_won': int(_sum_stat(stats, 'dribblesWon')),
                'tackles': int(_sum_stat(stats, 'tacklesTotal')),
                'tackles_won': int(_sum_stat(stats, 'tackleSuccessful')),
                'interceptions': int(_sum_stat(stats, 'interceptions')),
                'clearances': int(_sum_stat(stats, 'clearances')),
                'blocks': int(_sum_stat(stats, 'shotsBlocked')),
                'aerials': int(_sum_stat(stats, 'aerialsTotal')),
                'aerials_won': int(_sum_stat(stats, 'aerialsWon')),
                'ball_recoveries': _n(ev, ev['type'] == 'BallRecovery') if len(ev) else 0,
                'dispossessed': int(_sum_stat(stats, 'dispossessed')),
                'fouls': int(_sum_stat(stats, 'foulsCommited')),
                'fouled': _n(ev, (ev['type'] == 'Foul') & (ev['outcomeType'] == 'Successful')) if len(ev) else 0,
                'yellow_cards': yellows,
                'red_cards': reds,
                'saves': int(_sum_stat(stats, 'totalSaves')),
                'goals_conceded': 0,
                'avg_x': round(float(ev['x'].median()), 2) if len(ev) and ev['x'].notna().any() else None,
                'avg_y': round(float(ev['y'].median()), 2) if len(ev) and ev['y'].notna().any() else None,
            })

    # goals conceded for the two keepers
    for r in rows:
        if r['position'] == 'GK' and r['is_first_eleven']:
            r['goals_conceded'] = M.agoal_count if r['team_id'] == M.hteamID else M.hgoal_count
    return rows


# ---------------------------------------------------------------------------
# event / shot / pass-link level
# ---------------------------------------------------------------------------

def event_rows(M) -> list[dict]:
    out = []
    for i, d in enumerate(M.df.to_dict('records')):
        out.append({
            'opta_event_id': int(d['id']) if pd.notna(d.get('id')) else None,
            'event_index': i,
            'period': d.get('period'),
            'minute': _int(d.get('minute')),
            'second': _int(d.get('second')),
            'expanded_minute': _int(d.get('expandedMinute')),
            'team_id': _int(d.get('teamId')),
            'player_id': _int(d.get('playerId')),
            'type': d.get('type'),
            'outcome': d.get('outcomeType'),
            'x': _f(d.get('x')), 'y': _f(d.get('y')),
            'end_x': _f(d.get('endX')), 'end_y': _f(d.get('endY')),
            'goal_mouth_y': _f(d.get('goalMouthY')), 'goal_mouth_z': _f(d.get('goalMouthZ')),
            'is_touch': _b(d.get('isTouch')), 'is_shot': _b(d.get('isShot')), 'is_goal': _b(d.get('isGoal')),
            'card_type': _card(d.get('cardType')),
            'xt_start': _f(d.get('start_zone_value_xT'), 5),
            'xt_end': _f(d.get('end_zone_value_xT'), 5),
            'xt_delta': _f(d.get('xT'), 5),
            'progression_m': _f(d.get('pro')),
            'is_progressive': bool((d.get('pro') or 0) >= 9.144),
            'qualifiers': parse_qualifiers(d.get('qualifiers')),
        })
    return out


def shot_rows(M) -> list[dict]:
    S = M.Shotsdf
    out = []
    for _, r in S.iterrows():
        q = r['qualifiers'] or ''
        out.append({
            'event_index': int(M.df.index[M.df['id'] == r['id']][0]) if (M.df['id'] == r['id']).any() else None,
            'team_id': _int(r['teamId']),
            'player_id': _int(r['playerId']),
            'minute': _int(r['minute']),
            'outcome': r['type'],
            'is_goal': r['type'] == 'Goal',
            'is_own_goal': 'OwnGoal' in q,
            'is_big_chance': 'BigChance' in q,
            'is_penalty': 'Penalty' in q,
            'is_header': 'Head' in q,
            'body_part': _first_of(q, ['RightFoot', 'LeftFoot', 'Head', 'OtherBodyPart']),
            'situation': _first_of(q, ['RegularPlay', 'FromCorner', 'SetPiece', 'DirectFreekick',
                                       'FastBreak', 'Penalty', 'ThrowinSetPiece']),
            'x': _f(r['x']), 'y': _f(r['y']),
            'distance_m': round(float(np.hypot(105 - r['x'], 34 - r['y'])), 2),
            'goal_mouth_y': _f(r['goalMouthY']), 'goal_mouth_z': _f(r['goalMouthZ']),
            'xg': None,
        })
    return out


def pass_link_rows(M) -> list[dict]:
    """Directed passer -> receiver counts for completed passes."""
    p = M.passes_df
    p = p[p['outcomeType'] == 'Successful'].dropna(subset=['playerId', 'receiver'])
    teams = {int(r['playerId']): int(r['teamId']) for _, r in p.iterrows()}
    grouped = p.groupby(['playerId', 'receiver']).size().reset_index(name='pass_count')
    out = []
    for _, r in grouped.iterrows():
        passer, receiver = int(r['playerId']), int(r['receiver'])
        if teams.get(passer) != teams.get(receiver):
            continue  # the "receiver" was an opponent - not a real link
        out.append({
            'team_id': teams[passer],
            'passer_id': passer,
            'receiver_id': receiver,
            'pass_count': int(r['pass_count']),
        })
    return out


# ---------------------------------------------------------------------------

def _int(v):
    return int(v) if v is not None and pd.notna(v) else None


def _f(v, nd=2):
    return round(float(v), nd) if v is not None and pd.notna(v) else None


def _b(v):
    return bool(v) if v is not None and pd.notna(v) else None


def _card(v):
    return str(v) if v is not None and pd.notna(v) and str(v) != 'nan' else None


def _first_of(qualifiers: str, names: list[str]):
    for n in names:
        if f"'{n}'" in qualifiers:
            return n
    return None
