"""Turn warehouse rows into sentences worth retrieving.

The master prompt's rule is *do not embed raw tables*, and it is right. A row of
numbers embeds badly: "ppda=10.76, field_tilt=63.2" shares no vocabulary with
"did Arsenal press high against Leeds?". A sentence describing what those
numbers mean shares plenty.

Six document types, each answering a different shape of question:

    match      what happened, once per match
    tactical   how a team played, once per team per match
    pressing   how a team defended out of possession
    sequence   one passage of play worth describing
    shot       one chance and its quality
    player     one player's afternoon

Every generator is a pure function from rows to text. Nothing here reads the
network, nothing is random, and running it twice produces identical output —
which is what lets the whole corpus be thrown away and rebuilt whenever the
wording improves.

Two rules the text itself follows:

**Numbers are context, not the answer.** A document may say Arsenal had 63%
field tilt, but when a user asks for that number the agent fetches it from SQL.
Retrieval finds the *right passage of play*; the SQL tool supplies the figure.
If the two ever disagree, the SQL is right and the document is stale.

**Say what is unusual.** "Arsenal completed 342 passes" retrieves against
almost any query about Arsenal. "Arsenal built patiently from the back, 63% of
sequences starting in their own third" retrieves against questions about
build-up. Documents lean on comparisons and characterisations because that is
what distinguishes them from each other.
"""

from __future__ import annotations

# Thresholds that decide what is worth a sentence at all. A corpus of every
# possession would be 11,000 near-identical documents; the interesting ones are
# those that produced a shot or lasted long enough to have a shape.
SEQUENCE_MIN_EVENTS = 8
PLAYER_MIN_MINUTES = 20


def _fmt(value, nd=2, dash='unknown'):
    if value is None:
        return dash
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f'{f:.{nd}f}'.rstrip('0').rstrip('.') if nd else f'{f:.0f}'


def _plural(n, singular, plural=None):
    return f'{n} {singular if n == 1 else (plural or singular + "s")}'


def _was(n) -> str:
    return 'was' if n == 1 else 'were'


def _ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st, 32nd.

    Minute 0 is the clock's first minute, not a zeroth one — Opta counts from
    zero and football does not.
    """
    n = int(n)
    if n <= 0:
        return 'opening'
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


# Opta type names are not English. These appear in "the possession ended
# with ..." so they need to read as an outcome, not a label.
_END_TYPE_PROSE = {
    'Pass': 'a misplaced pass',
    'BallTouch': 'a poor touch',
    'Dispossessed': 'the ball being won back',
    'TakeOn': 'a failed dribble',
    'Foul': 'a foul',
    'MissedShots': 'a shot off target',
    'SavedShot': 'a save',
    'ShotOnPost': 'a shot against the woodwork',
    'Goal': 'a goal',
    'Tackle': 'a tackle',
    'Interception': 'an interception',
    'Clearance': 'a clearance',
    'Aerial': 'an aerial duel',
    'CornerAwarded': 'the ball going out of play',
    'OffsidePass': 'an offside',
    'Card': 'a stoppage',
    'Save': 'a save',
    'KeeperPickup': 'the goalkeeper collecting it',
    'Challenge': 'a challenge',
    'BlockedPass': 'a blocked pass',
    'End': 'the end of the half',
}


def _readable_end(value: str) -> str:
    return _END_TYPE_PROSE.get(value, f'a {value}' if value else 'possession being lost')


def score_state(goals: list[tuple[int, int]], team_id, minute: int) -> str:
    """leading / level / trailing for `team_id` at `minute`.

    `goals` is (minute, scoring_team_id) for the whole match. Score state is
    the single most useful filter on a tactical question — a side chasing a
    game plays nothing like the same side protecting a lead — so it is metadata
    on every document that has a minute.
    """
    ours = sum(1 for m, t in goals if m <= minute and t == team_id)
    theirs = sum(1 for m, t in goals if m <= minute and t != team_id)
    if ours > theirs:
        return 'leading'
    if ours < theirs:
        return 'trailing'
    return 'level'


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------

def match_document(match: dict) -> str:
    home, away = match['home_team_name'], match['away_team_name']
    hs, as_ = match['home_score'], match['away_score']
    result = ('a draw' if hs == as_
              else f'a {max(hs, as_)}-{min(hs, as_)} win for {home if hs > as_ else away}')

    lines = [
        f"{home} {hs}-{as_} {away}, {match['competition']} "
        f"matchweek {match['matchweek'] or '?'}, {match['match_date']}"
        f"{', ' + match['venue_name'] if match.get('venue_name') else ''}. "
        f"The match finished {result}."
    ]

    if match.get('ht_home_score') is not None:
        lines.append(f"Half time: {match['ht_home_score']}-{match['ht_away_score']}.")

    if match.get('home_xg') is not None:
        lines.append(
            f"Expected goals {_fmt(match['home_xg'])} to {_fmt(match['away_xg'])}"
            + (f", expected goals on target {_fmt(match['home_xgot'])} to "
               f"{_fmt(match['away_xgot'])}" if match.get('home_xgot') is not None
               else '')
            + '.')
        lines.append(_overperformance(home, away, hs, as_,
                                      match['home_xg'], match['away_xg']))

    if match.get('home_possession') is not None:
        lines.append(
            f"{home} had {_fmt(match['home_possession'], 0)}% of the ball to "
            f"{_fmt(match['away_possession'], 0)}%, and took "
            f"{_plural(match['home_shots'] or 0, 'shot')} to "
            f"{match['away_shots'] or 0}.")

    if match.get('referee'):
        lines.append(f"Referee: {match['referee']}."
                     + (f" Attendance {match['attendance']:,}."
                        if match.get('attendance') else ''))

    return ' '.join(l for l in lines if l)


def _overperformance(home, away, hs, as_, hxg, axg) -> str:
    """Whether the scoreline flattered anyone — the thing people actually ask."""
    try:
        hxg, axg = float(hxg), float(axg)
    except (TypeError, ValueError):
        return ''
    gap_home, gap_away = hs - hxg, as_ - axg
    if abs(gap_home) < 1 and abs(gap_away) < 1:
        return 'Both sides scored close to what their chances were worth.'
    parts = []
    if gap_home >= 1:
        parts.append(f'{home} scored {_fmt(gap_home, 1)} more than their chances '
                     'were worth, finishing clinically')
    elif gap_home <= -1:
        parts.append(f'{home} scored {_fmt(-gap_home, 1)} fewer than their chances '
                     'were worth, wasteful in front of goal')
    if gap_away >= 1:
        parts.append(f'{away} scored {_fmt(gap_away, 1)} more than their chances '
                     'were worth')
    elif gap_away <= -1:
        parts.append(f'{away} scored {_fmt(-gap_away, 1)} fewer than their chances '
                     'were worth')
    return ('; '.join(parts) + '.') if parts else ''


# ---------------------------------------------------------------------------
# tactical / pressing
# ---------------------------------------------------------------------------

def tactical_document(row: dict) -> str:
    team, opponent = row['team_name'], row['opponent_name']
    where = 'at home to' if row['is_home'] else 'away at'

    lines = [f"How {team} played {where} {opponent} on {row['match_date']} "
             f"({row['home_score']}-{row['away_score']})."]

    tilt = row.get('field_tilt_pct')
    if tilt is not None:
        tilt = float(tilt)
        character = ('dominated territory' if tilt >= 60
                     else 'were pinned back' if tilt <= 40
                     else 'shared territory evenly')
        lines.append(f"{team} {character}, with {_fmt(tilt, 1)}% of final-third "
                     f"touches.")

    seq = row.get('avg_sequence_events')
    if seq is not None:
        speed = row.get('direct_speed_ms')
        style = ('patient, working the ball through long possessions'
                 if float(seq) >= 5.5 else
                 'direct, moving the ball forward quickly'
                 if float(seq) <= 3.5 else 'balanced between patience and directness')
        lines.append(
            f"Their build-up was {style}: {_fmt(seq)} events per possession on "
            f"average over {row['sequences']} sequences, "
            f"{row['long_sequences']} of them ten passages or longer, advancing "
            f"{_fmt(speed, 2)} metres upfield per second of possession.")

    if row.get('build_from_defensive_third') is not None and row.get('sequences'):
        share = 100 * row['build_from_defensive_third'] / row['sequences']
        lines.append(
            f"{_fmt(share, 0)}% of their possessions began in their own third, "
            f"and {row['build_from_high_turnover']} began in the attacking third "
            f"after winning the ball high.")

    if row.get('sequences_ending_in_shot') is not None and row.get('sequences'):
        per = row['sequences'] / max(row['sequences_ending_in_shot'], 1)
        lines.append(
            f"{row['sequences_ending_in_shot']} possessions ended in a shot, "
            f"one every {_fmt(per, 1)} possessions.")

    return ' '.join(lines)


def pressing_document(row: dict) -> str:
    team, opponent = row['team_name'], row['opponent_name']
    ppda = row.get('ppda')

    lines = [f"How {team} defended without the ball against {opponent} on "
             f"{row['match_date']}."]

    if ppda is not None:
        ppda = float(ppda)
        # The scale in docs/FOOTBALL_DATA_COOKBOOK.md: elite pressing 7-9,
        # passive 15+.
        intensity = ('an intense, aggressive press' if ppda < 9
                     else 'a committed press' if ppda < 12
                     else 'a moderate press' if ppda < 15
                     else 'a passive, low block')
        lines.append(
            f"{team} applied {intensity}, allowing {_fmt(ppda)} opposition passes "
            f"per defensive action in the pressing zone (PPDA) from "
            f"{row['def_actions_opp_half']} defensive actions there. "
            f"Lower PPDA means more intense pressing.")

        allowed = row.get('ppda_allowed')
        if allowed is not None:
            allowed = float(allowed)
            comparison = ('pressed harder than' if ppda < allowed - 1
                          else 'were pressed harder than' if ppda > allowed + 1
                          else 'pressed about as hard as')
            lines.append(f"{team} {comparison} {opponent}, whose PPDA was "
                         f"{_fmt(allowed)}.")

    if row.get('counterpresses') is not None:
        lines.append(
            f"They counterpressed {row['counterpresses']} times — defensive "
            f"actions within five seconds of losing the ball — and won it back "
            f"in the attacking third {_plural(row['high_turnovers'] or 0, 'time')}.")

    return ' '.join(lines)


# ---------------------------------------------------------------------------
# sequence / shot / player
# ---------------------------------------------------------------------------

def sequence_document(row: dict) -> str:
    team = row['team_name']
    started = {'defensive': 'deep in their own third',
               'middle': 'in midfield',
               'attacking': 'high up the pitch'}.get(row['build_zone'], '')

    opening = (f"{team} won the ball {started} in the "
               f"{_ordinal(row['minute'])} minute against {row['opponent_name']} "
               f"({row['match_date']}) and kept it for "
               f"{_plural(row['events'], 'touch', 'touches')} over "
               f"{_fmt(row['duration_s'], 0)} seconds, involving "
               f"{_plural(row['players'], 'player')}.")

    parts = [opening]

    if row.get('is_high_turnover'):
        parts.append('It began with a high turnover, winning possession in the '
                     'attacking third.')

    progression = row.get('progression_m')
    if progression is not None and float(progression) > 20:
        parts.append(f"The move carried the ball {_fmt(progression, 0)} metres "
                     f"upfield.")

    if row.get('ended_in_goal'):
        parts.append('The possession ended in a goal.')
    elif row.get('ended_in_shot'):
        parts.append('The possession ended in a shot.')
    else:
        parts.append(f"It broke down with {_readable_end(row['end_type'])}.")

    if row.get('passes'):
        parts.append(f"{_plural(row['passes'], 'pass', 'passes')} "
                     f"{_was(row['passes'])} completed in the move.")

    return ' '.join(parts)


def shot_document(row: dict) -> str:
    who = row.get('player_name') or 'A player'
    outcome = {'Goal': 'scored', 'SavedShot': 'forced a save',
               'MissedShots': 'missed the target',
               'ShotOnPost': 'hit the woodwork'}.get(row['outcome'], 'shot')

    parts = [f"{who} of {row['team_name']} {outcome} in the "
             f"{_ordinal(row['minute'])} minute against {row['opponent_name']} "
             f"on {row['match_date']}, from {_fmt(row['distance_m'], 0)} metres."]

    descriptors = []
    if row.get('is_big_chance'):
        descriptors.append('a big chance')
    if row.get('is_penalty'):
        descriptors.append('a penalty')
    if row.get('is_header'):
        descriptors.append('a header')
    if row.get('situation') and row['situation'] != 'RegularPlay':
        descriptors.append(f"from {_readable_situation(row['situation'])}")
    if descriptors:
        parts.append('It was ' + ', '.join(descriptors) + '.')

    if row.get('body_part') and not row.get('is_header'):
        parts.append(f"Struck with the {_readable_body(row['body_part'])}.")

    return ' '.join(parts)


def _readable_situation(value: str) -> str:
    return {'FromCorner': 'a corner', 'SetPiece': 'a set piece',
            'DirectFreekick': 'a direct free kick', 'FastBreak': 'a fast break',
            'Penalty': 'a penalty',
            'ThrowinSetPiece': 'a throw-in'}.get(value, value)


def _readable_body(value: str) -> str:
    return {'RightFoot': 'right foot', 'LeftFoot': 'left foot',
            'Head': 'head', 'OtherBodyPart': 'another body part'}.get(value, value)


def player_document(row: dict) -> str:
    who, team = row['player_name'], row['team_name']
    role = 'started' if row.get('is_first_eleven') else 'came off the bench'

    parts = [f"{who} ({team}, {row.get('position') or 'unknown position'}) {role} "
             f"against {row['opponent_name']} on {row['match_date']} and played "
             f"{_plural(row['minutes_played'] or 0, 'minute')}."]

    contributions = []
    if row.get('goals'):
        contributions.append(_plural(row['goals'], 'goal'))
    if row.get('assists'):
        contributions.append(_plural(row['assists'], 'assist'))
    if row.get('key_passes'):
        contributions.append(_plural(row['key_passes'], 'key pass', 'key passes'))
    if contributions:
        parts.append('They contributed ' + ', '.join(contributions) + '.')

    if row.get('passes'):
        accuracy = (100 * row['passes_accurate'] / row['passes']
                    if row.get('passes_accurate') else None)
        parts.append(
            f"{_plural(row['passes'], 'pass', 'passes')} attempted"
            + (f" at {_fmt(accuracy, 0)}% accuracy" if accuracy else '')
            + (f", {row['progressive_passes']} of them progressive"
               if row.get('progressive_passes') else '') + '.')

    if row.get('carries'):
        parts.append(
            f"They carried the ball {_plural(row['carries'], 'time')}, "
            f"{row.get('progressive_carries') or 0} of those progressive, "
            f"for {_fmt(row.get('carry_progression_m'), 0)} metres of "
            f"forward progress.")

    defensive = []
    for field, word in (('tackles', 'tackle'), ('interceptions', 'interception'),
                        ('ball_recoveries', 'recovery', ), ('clearances', 'clearance')):
        if row.get(field):
            defensive.append(_plural(row[field], word))
    if defensive:
        parts.append('Defensively: ' + ', '.join(defensive) + '.')

    if row.get('xt') is not None and float(row['xt'] or 0) != 0:
        parts.append(f"Their actions added {_fmt(row['xt'], 3)} expected threat.")

    if row.get('rating'):
        parts.append(f"Match rating {_fmt(row['rating'], 2)}.")

    return ' '.join(parts)
