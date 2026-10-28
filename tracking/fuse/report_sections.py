"""Fused markdown report: event warehouse + tracking insights."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tracking.paths import output_report_path
from tracking.store.load_db import fetch_event_summary


def write_tracking_report(match_id: int, insights: dict[str, Any]) -> Path:
    event = fetch_event_summary(match_id)
    match = event.get('match') or {}
    lines: list[str] = []
    home = match.get('home_team') or 'Home'
    away = match.get('away_team') or 'Away'
    lines.append(f'# Tracking + event report — {home} vs {away}')
    lines.append('')
    lines.append(f'match_id `{match_id}`')
    lines.append('')

    lines.append('## Event warehouse')
    lines.append('')
    if match:
        lines.append(
            f'- **Score:** {home} {match.get("home_score")}–{match.get("away_score")} {away}'
        )
        lines.append(
            f'- **xG:** {home} {match.get("home_xg")} · {away} {match.get("away_xg")}'
        )
        lines.append(f'- **Date / GW:** {match.get("match_date")} · GW {match.get("matchweek")}')
    else:
        lines.append('- Event match row not available (DB offline or match missing).')
    for ts in event.get('team_stats') or []:
        lines.append(
            f'- **{ts.get("name")}:** poss {ts.get("possession_pct")} · '
            f'shots {ts.get("shots")} ({ts.get("shots_on_target")} on target)'
        )
    lines.append('')

    lines.append('## Tracking — team shape')
    lines.append('')
    for t in insights.get('teams') or []:
        lines.append(
            f'- **{t.get("team")}:** line height {t.get("avg_line_height_m")} m · '
            f'width {t.get("avg_width_m")} m · compactness {t.get("avg_compactness")} · '
            f'thirds D/M/A '
            f'{t.get("share_def_third")}/{t.get("share_mid_third")}/{t.get("share_att_third")}'
        )
    if not insights.get('teams'):
        lines.append('- No team metrics (empty tracks).')
    lines.append('')

    lines.append('## Tracking — player workloads (anonymous track IDs)')
    lines.append('')
    players = sorted(
        insights.get('players') or [],
        key=lambda r: r.get('distance_m') or 0,
        reverse=True,
    )[:12]
    if players:
        lines.append('| track_id | team | distance_m | sprints | avg_x | avg_y |')
        lines.append('|---|---|---|---|---|---|')
        for p in players:
            lines.append(
                f'| {p.get("track_id")} | {p.get("team")} | {p.get("distance_m")} | '
                f'{p.get("sprint_count")} | {p.get("avg_x_m")} | {p.get("avg_y_m")} |'
            )
    else:
        lines.append('- No player metrics.')
    lines.append('')

    lines.append('## Phases')
    lines.append('')
    phases = insights.get('phases') or []
    if phases:
        lines.append('| team | phase | t_start_s | t_end_s |')
        lines.append('|---|---|---|---|')
        for ph in phases[:40]:
            lines.append(
                f'| {ph.get("team")} | {ph.get("phase")} | '
                f'{ph.get("t_start_s")} | {ph.get("t_end_s")} |'
            )
        if len(phases) > 40:
            lines.append(f'| … | ({len(phases) - 40} more) | | |')
    else:
        lines.append('- No phases segmented.')
    lines.append('')

    lines.append('## Notes')
    lines.append('')
    lines.append(f'- Tracks: `{insights.get("tracks_path")}`')
    lines.append(f'- DB load ok: `{insights.get("db_ok")}`')
    lines.append(
        '- Player identities are track IDs only in v1; jersey OCR is out of scope.'
    )
    lines.append('')

    out = output_report_path(match_id)
    out.write_text('\n'.join(lines), encoding='utf-8')
    return out
