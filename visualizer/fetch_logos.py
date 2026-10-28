#!/usr/bin/env python3
"""Download a crest for every club Arsenal face this season.

Reads the fixture calendar, resolves each club on SofaScore, and saves its crest
into `visualizer/assets/` under a slug the renderers already look for, so panels
never fall back to a blank header.

    python visualizer/fetch_logos.py                 # all clubs in the calendar
    python visualizer/fetch_logos.py --force         # re-download existing
    python visualizer/fetch_logos.py --team "Girona FC"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, 'visualizer', 'assets')
FIXTURES = os.path.join(ROOT, 'airflow', 'fixtures', 'arsenal_2026_27.json')
MANIFEST = os.path.join(ASSETS, 'logos.json')

# SofaScore's search is fuzzy; pin the ones where the top hit is the wrong club
# (reserves, women's sides, or a same-named club elsewhere). Every id here is
# verified against /team/<id> before use - see verify_team_id().
SEARCH_OVERRIDES = {
    'Arsenal': 42,
    'Manchester United': 35,
    'Manchester City': 17,
    'Tottenham Hotspur': 33,
    'Newcastle United': 39,
    'Nottingham Forest': 14,
    'Brighton & Hove Albion': 30,
    'AFC Bournemouth': 60,
    'Leeds United': 34,
    'Hull City': 96,
    'Ipswich Town': 32,
    'Coventry City': 11,
}

# Ignore these words when comparing a club's name to SofaScore's.
NOISE = {'fc', 'afc', 'united', 'city', 'hove', 'albion', 'the', '&', 'and'}


def _tokens(name: str) -> set[str]:
    cleaned = name.lower().replace('&', ' ').replace('.', ' ').replace('-', ' ')
    return {t for t in cleaned.split() if t not in NOISE} or set(cleaned.split())


def verify_team_id(client, team_id: int, expected: str) -> tuple[bool, str]:
    """Confirm an id really is the club we mean.

    Two of these were wrong on the first pass (Coventry resolved to Chesterfield,
    Hull to Derby County) and only a visual check caught it. Now the crest is
    never written unless the name matches.
    """
    try:
        data = client._get(f'https://api.sofascore.com/api/v1/team/{team_id}')
    except Exception as exc:                                     # noqa: BLE001
        return False, f'lookup failed: {exc}'

    team = (data or {}).get('team') or {}
    actual = team.get('name') or ''
    if not actual:
        return False, 'no team returned'
    if team.get('gender', 'M') != 'M':
        return False, f'{actual} is not the mens team'

    wanted, found = _tokens(expected), _tokens(actual)
    if wanted & found:
        return True, actual
    return False, f'resolved to {actual!r}, expected {expected!r}'


def slug_for(team_name: str) -> str:
    """Must match visualizer.match_report.load_logo's fallback."""
    return team_name.lower().replace(' & ', '-').replace(' ', '_') + '.png'


def resolve_team_id(client, name: str) -> int | None:
    if name in SEARCH_OVERRIDES:
        return SEARCH_OVERRIDES[name]
    try:
        data = client._get(
            f'https://api.sofascore.com/api/v1/search/all?q={name.replace(" ", "%20")}&page=0')
    except Exception:                                            # noqa: BLE001
        return None
    for result in (data or {}).get('results', []):
        entity = result.get('entity', {})
        if result.get('type') == 'team' and (entity.get('sport') or {}).get('name') == 'Football':
            if entity.get('gender', 'M') == 'M':
                return entity.get('id')
    return None


def download(client, team_id: int, path: str) -> bool:
    url = f'https://api.sofascore.com/api/v1/team/{team_id}/image'
    try:
        resp = client.session.get(url, timeout=25)
        resp.raise_for_status()
        if len(resp.content) < 500:
            return False
        with open(path, 'wb') as fh:
            fh.write(resp.content)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def clubs_from_calendar() -> list[str]:
    with open(FIXTURES) as fh:
        fixtures = json.load(fh)['fixtures']
    names = {'Arsenal'}
    for f in fixtures:
        names.add(f['opponent'])
    return sorted(names)


def main():
    from scraper.sofascore import SofaScoreClient

    ap = argparse.ArgumentParser()
    ap.add_argument('--team', action='append', help='specific club(s) instead of the calendar')
    ap.add_argument('--force', action='store_true', help='re-download crests that already exist')
    args = ap.parse_args()

    os.makedirs(ASSETS, exist_ok=True)
    client = SofaScoreClient()
    names = args.team or clubs_from_calendar()

    manifest = {}
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as fh:
            manifest = json.load(fh)

    got = skipped = failed = 0
    for name in names:
        path = os.path.join(ASSETS, slug_for(name))
        if os.path.exists(path) and not args.force:
            print(f'  = {name:26s} already have {os.path.basename(path)}')
            manifest.setdefault(name, {'file': os.path.basename(path)})
            skipped += 1
            continue

        team_id = resolve_team_id(client, name)
        if not team_id:
            print(f'  ! {name:26s} could not resolve on SofaScore')
            failed += 1
            continue

        ok, detail = verify_team_id(client, team_id, name)
        if not ok:
            print(f'  ! {name:26s} id={team_id:<6} REJECTED - {detail}')
            failed += 1
            continue

        if download(client, team_id, path):
            print(f'  + {name:26s} id={team_id:<6} -> {os.path.basename(path)}  ({detail})')
            manifest[name] = {'file': os.path.basename(path), 'sofascore_team_id': team_id,
                              'verified_name': detail}
            got += 1
        else:
            print(f'  ! {name:26s} id={team_id:<6} download failed')
            failed += 1
        time.sleep(0.3)

    with open(MANIFEST, 'w') as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)

    print(f'\n{got} downloaded, {skipped} already present, {failed} failed '
          f'({len(manifest)} clubs in {os.path.relpath(MANIFEST, ROOT)})')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
