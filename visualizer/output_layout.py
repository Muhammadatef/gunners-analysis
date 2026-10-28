"""Where rendered match reports live on disk.

`final_output/` was fine for one match; across a 38-game season it would be
overwritten every week. This gives every match its own directory, sorted so a
plain `ls` reads in fixture order.

Clubs other than Arsenal nest one level deeper, so each keeps its own index and
`_latest` symlink:

    output/
      barcelona/
        2025-2026/
          _index.json
          mw06_2025-11-26_barcelona_v_chelsea/

Arsenal's own output predates the club level and stays where it is:

    output/
      2026-2027/
        _index.json                       every rendered match, newest first
        _latest -> mw01_2026-08-21_...    symlink to the most recent render
        mw01_2026-08-21_arsenal_v_coventry/
          00_dashboard.png
          match.json                      scoreline, source, panel list, stats
          panels/
            01_home_pass_network.png
            ...
            18_away_chance_creating.png
      friendlies-2026/
        f_2026-08-01_girona_v_arsenal/
          00_dashboard_sofascore.png
          panels/...

Directory names are stable and idempotent: re-rendering a match overwrites its
own folder and touches nothing else.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT_ROOT = os.path.join(ROOT, 'output')

INDEX_NAME = '_index.json'
LATEST_LINK = '_latest'
PANELS_DIR = 'panels'
DASHBOARD_PREFIX = '00_dashboard'


def slugify(value: str) -> str:
    """'Brighton & Hove Albion' -> 'brighton-hove-albion'"""
    value = unicodedata.normalize('NFKD', str(value))
    value = value.encode('ascii', 'ignore').decode('ascii').lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-') or 'unknown'


def season_dir_name(season: str | None, competition: str | None = None) -> str:
    """'2026/2027' -> '2026-2027'; a friendly with no season -> 'friendlies-<year>'."""
    if season:
        return slugify(season)
    if competition and 'friend' in competition.lower():
        return f'friendlies-{datetime.now(timezone.utc).year}'
    return slugify(competition or 'other')


def match_dir_name(date: str, home: str, away: str, matchweek: int | None = None) -> str:
    prefix = f'mw{int(matchweek):02d}' if matchweek else 'f'
    return f'{prefix}_{date}_{slugify(home)}_v_{slugify(away)}'


def club_root(output_root: str | None = None, club: str | None = None) -> str:
    """Where one club's seasons live. `club=None` is the legacy flat layout."""
    root = output_root or os.getenv('GUNNERS_OUTPUT_ROOT') or DEFAULT_OUTPUT_ROOT
    return os.path.join(root, slugify(club)) if club else root


def season_root(season: str | None, competition: str | None = None,
                output_root: str | None = None, club: str | None = None) -> str:
    return os.path.join(club_root(output_root, club),
                        season_dir_name(season, competition))


def match_dir(season: str | None, date: str, home: str, away: str,
              matchweek: int | None = None, competition: str | None = None,
              output_root: str | None = None, create: bool = True,
              club: str | None = None) -> str:
    path = os.path.join(season_root(season, competition, output_root, club),
                        match_dir_name(date, home, away, matchweek))
    if create:
        os.makedirs(os.path.join(path, PANELS_DIR), exist_ok=True)
    return path


def panels_dir(match_path: str) -> str:
    path = os.path.join(match_path, PANELS_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def write_manifest(match_path: str, manifest: dict) -> str:
    """Per-match match.json - what was rendered, from which source."""
    manifest = dict(manifest)
    manifest['rendered_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    manifest['panels'] = sorted(
        f for f in os.listdir(panels_dir(match_path)) if f.endswith('.png'))
    # the two renderers name their dashboard differently; both start with '00_'
    dashboards = sorted(f for f in os.listdir(match_path)
                        if f.startswith('00_') and f.endswith('.png'))
    manifest['dashboard'] = dashboards[0] if dashboards else None

    path = os.path.join(match_path, 'match.json')
    with open(path, 'w') as fh:
        json.dump(manifest, fh, indent=2)
    return path


def update_season_index(match_path: str) -> str:
    """Rebuild <season>/_index.json from the match.json files present."""
    season_path = os.path.dirname(match_path)
    entries = []
    for name in sorted(os.listdir(season_path)):
        # Skip the convenience symlink. On any render after the first it
        # already points at a real match directory, so `_latest/match.json`
        # resolves and `_latest` would be indexed as a match of its own - then
        # win the (date, matchweek) tie on name order and become the symlink
        # target, leaving `_latest -> _latest`.
        if name == LATEST_LINK or os.path.islink(os.path.join(season_path, name)):
            continue
        manifest_path = os.path.join(season_path, name, 'match.json')
        if not os.path.isfile(manifest_path):
            continue
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        entries.append({
            'dir': name,
            'matchweek': manifest.get('matchweek'),
            'date': manifest.get('date'),
            'home': manifest.get('home_team'),
            'away': manifest.get('away_team'),
            'score': manifest.get('score'),
            'source': manifest.get('source'),
            'panel_count': len(manifest.get('panels') or []),
            'rendered_at': manifest.get('rendered_at'),
        })
    entries.sort(key=lambda e: (e.get('date') or '', e.get('matchweek') or 0), reverse=True)

    index_path = os.path.join(season_path, INDEX_NAME)
    with open(index_path, 'w') as fh:
        json.dump({'season': os.path.basename(season_path),
                   'matches': entries,
                   'updated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')},
                  fh, indent=2)
    _link_latest(season_path, entries)
    return index_path


def _link_latest(season_path: str, entries: list) -> None:
    if not entries:
        return
    link = os.path.join(season_path, LATEST_LINK)
    target = entries[0]['dir']
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(target, link)
    except OSError:
        pass          # symlinks may be unavailable (e.g. a Windows bind mount)


def find_match_dir(job_id: str, output_root: str | None = None) -> str | None:
    """Locate the directory a job was rendered into, or None.

    Directory names are built from the fixture, not the job id, so the only way
    back is the `job_id` each match.json records. The tree is one season (or
    club/season) deep and holds tens of directories, so a walk is cheap - and it
    works for `--no-load` runs, which have no warehouse row to look up.
    """
    root = output_root or os.getenv('GUNNERS_OUTPUT_ROOT') or DEFAULT_OUTPUT_ROOT
    if not os.path.isdir(root):
        return None

    for dirpath, dirnames, filenames in os.walk(root):
        # `_latest` points at a real match directory; following it would report
        # the symlink's path instead of the match's own.
        dirnames[:] = [d for d in dirnames
                       if d != LATEST_LINK and not os.path.islink(os.path.join(dirpath, d))]
        if 'match.json' not in filenames:
            continue
        try:
            with open(os.path.join(dirpath, 'match.json')) as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            continue
        if manifest.get('job_id') == job_id:
            return dirpath
    return None


def match_panels(match_path: str) -> list:
    """Every rendered PNG for a match: the dashboard, then the panels."""
    out = sorted(f for f in os.listdir(match_path)
                 if f.startswith('00_') and f.endswith('.png'))
    panels = os.path.join(match_path, PANELS_DIR)
    if os.path.isdir(panels):
        out += sorted(f for f in os.listdir(panels) if f.endswith('.png'))
    return out


def finalise(match_path: str, manifest: dict) -> dict:
    """Write the manifest and refresh the season index in one call."""
    write_manifest(match_path, manifest)
    index_path = update_season_index(match_path)
    return {'match_dir': match_path, 'index': index_path}
