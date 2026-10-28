"""SofaScore data source - fallback for matches WhoScored does not cover.

Why this exists
---------------
WhoScored is the primary source because it publishes a full Opta event stream
(`matchCentreData`): ~1400 events with start/end coordinates, which is what the
18-panel dashboard is built from. For friendlies and lower-tier competitions it
publishes only a summary feed and the dashboard cannot be built at all.

SofaScore covers those matches, but through a *different* shape: rich per-player
aggregates, a shot map with coordinates, per-player touch heatmaps and average
positions - but **no pass-by-pass event stream**. So it can reconstruct a
meaningful subset of the panels, not all of them. See
`visualizer/sofascore_report.py` for exactly which.

It is a plain JSON API, so no Selenium is needed here.

    from scraper.sofascore import SofaScoreClient
    c = SofaScoreClient()
    event_id = c.resolve_event_id('https://www.sofascore.com/football/match/girona-fc-arsenal/RsoKj')
    bundle = c.fetch_match(event_id, with_heatmaps=True)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Direct api.* is often 403 from datacenter IPs; same-origin www path works more often.
API_HOSTS = (
    'https://www.sofascore.com/api/v1',
    'https://api.sofascore.com/api/v1',
)
WEB = 'https://www.sofascore.com'

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
# https://www.sofascore.com/football/match/<slug>/<customId>[#id:<eventId>]
URL_RE = re.compile(r'sofascore\.com/(?:football/)?match/([a-z0-9\-]+)/([A-Za-z0-9]+)')
EVENT_ID_RE = re.compile(r'#id:(\d+)')

# Only used as find_event's default - that search walks one club's calendar,
# and this repo's calendar is Arsenal's.
ARSENAL_SOFASCORE_TEAM_ID = 42


class SofaScoreError(RuntimeError):
    pass


class SofaScoreClient:
    """Thin, polite client over SofaScore's public JSON API."""

    def __init__(self, timeout: int = 25, retries: int = 3, pause: float = 0.25):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-GB,en;q=0.9',
            'Referer': f'{WEB}/',
        })

    # -- low level ---------------------------------------------------------

    def _get(self, url: str, as_json: bool = True) -> Any:
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    return None
                # Cloudflare / Varnish bot walls rarely clear on retry — fail this
                # host quickly so FotMob (or the next API host) can take over.
                if resp.status_code == 403:
                    raise SofaScoreError(f'403 Forbidden for url: {url}')
                resp.raise_for_status()
                time.sleep(self.pause)
                return resp.json() if as_json else resp.text
            except SofaScoreError:
                raise
            except Exception as exc:                             # noqa: BLE001
                last = exc
                logger.warning('sofascore GET %s failed (%d/%d): %s',
                               url, attempt, self.retries, exc)
                time.sleep(1.5 * attempt)
        raise SofaScoreError(f'GET {url} failed after {self.retries} attempts: {last}')

    def _endpoint(self, event_id: int, path: str = '') -> Any:
        last = None
        for base in API_HOSTS:
            try:
                return self._get(f'{base}/event/{event_id}{path}')
            except SofaScoreError as exc:
                last = exc
                logger.warning('sofascore host %s failed for event %s%s: %s',
                               base, event_id, path, exc)
        raise SofaScoreError(f'event {event_id}{path} failed on all hosts: {last}')

    # -- resolution --------------------------------------------------------

    def resolve_event_id(self, url: str) -> int:
        """Turn a SofaScore match URL into its numeric event id.

        The public URL carries a short `customId` (e.g. `RsoKj`), which the API
        does not accept. The numeric id lives in the page's __NEXT_DATA__ blob.
        """
        direct = EVENT_ID_RE.search(url)
        if direct:
            return int(direct.group(1))

        match = URL_RE.search(url)
        if not match:
            raise SofaScoreError(f'not a SofaScore match URL: {url}')
        slug, custom_id = match.group(1), match.group(2)

        html = self._get(url, as_json=False)
        blob = NEXT_DATA_RE.search(html or '')
        if not blob:
            raise SofaScoreError(f'__NEXT_DATA__ not found on {url}')

        for event in _walk_events(json.loads(blob.group(1))):
            if event.get('customId') == custom_id and 'id' in event:
                logger.info('resolved %s (%s) -> event %s', slug, custom_id, event['id'])
                return int(event['id'])
        raise SofaScoreError(f'no event with customId={custom_id} on {url}')

    # -- match payloads ----------------------------------------------------

    def fetch_match(self, event_id: int, with_heatmaps: bool = True) -> Dict[str, Any]:
        """Pull every endpoint that carries analysable data for one match."""
        logger.info('fetching sofascore event %s', event_id)
        bundle: Dict[str, Any] = {
            'source': 'sofascore',
            'event_id': event_id,
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }

        event = self._endpoint(event_id)
        if not event or 'event' not in event:
            raise SofaScoreError(f'event {event_id} not found')
        bundle['event'] = event['event']

        for key, path in (
            ('lineups', '/lineups'),
            ('incidents', '/incidents'),
            ('statistics', '/statistics'),
            ('shotmap', '/shotmap'),
            ('graph', '/graph'),
            ('average_positions', '/average-positions'),
            ('best_players', '/best-players/summary'),
            ('managers', '/managers'),
        ):
            try:
                bundle[key] = self._endpoint(event_id, path)
            except SofaScoreError as exc:
                logger.warning('optional endpoint %s unavailable: %s', path, exc)
                bundle[key] = None

        if with_heatmaps and bundle.get('lineups'):
            bundle['heatmaps'] = self._fetch_heatmaps(event_id, bundle['lineups'])

        return bundle

    def _fetch_heatmaps(self, event_id: int, lineups: Dict[str, Any]) -> Dict[str, List[dict]]:
        """Per-player touch coordinates - one request per player."""
        out: Dict[str, List[dict]] = {}
        for side in ('home', 'away'):
            for entry in (lineups.get(side) or {}).get('players', []):
                pid = entry.get('player', {}).get('id')
                if not pid or not entry.get('statistics'):
                    continue                       # unused sub, no touches
                try:
                    data = self._endpoint(event_id, f'/player/{pid}/heatmap')
                except SofaScoreError:
                    continue
                if data and data.get('heatmap'):
                    out[str(pid)] = data['heatmap']
        logger.info('fetched %d player heatmaps', len(out))
        return out

    # -- convenience -------------------------------------------------------

    def fetch_by_url(self, url: str, with_heatmaps: bool = True) -> Dict[str, Any]:
        return self.fetch_match(self.resolve_event_id(url), with_heatmaps=with_heatmaps)

    def team_events(self, team_id: int, page: int = 0, past: bool = True) -> List[dict]:
        """A team's fixtures - used to find a match id without a URL."""
        kind = 'last' if past else 'next'
        last = None
        for base in API_HOSTS:
            try:
                data = self._get(f'{base}/team/{team_id}/events/{kind}/{page}')
                return (data or {}).get('events', [])
            except SofaScoreError as exc:
                last = exc
        raise SofaScoreError(f'team events failed on all hosts: {last}')

    def find_event(self, date: str, home: str, away: str,
                   team_id: int = ARSENAL_SOFASCORE_TEAM_ID, pages: int = 3) -> Optional[int]:
        """Locate a match by date + teams, without needing its URL.

        `team_id` defaults to Arsenal since that is whose calendar we walk - so
        this search is Arsenal-only unless a caller supplies another club's id.
        For any other club, resolve the event from its URL instead
        (`resolve_event_id`); walking Arsenal's calendar for a Barcelona match
        finds nothing at best and a same-date coincidence at worst.
        Matching is on date plus a token overlap on both club names, so
        'Brighton & Hove Albion' still matches SofaScore's 'Brighton'.
        """
        import datetime

        for page in range(pages):
            for event in self.team_events(team_id, page=page, past=True):
                stamp = event.get('startTimestamp')
                if not stamp:
                    continue
                when = datetime.datetime.fromtimestamp(
                    stamp, datetime.timezone.utc).strftime('%Y-%m-%d')
                if when != date:
                    continue
                if (_name_matches(home, event['homeTeam']['name'])
                        and _name_matches(away, event['awayTeam']['name'])):
                    logger.info('matched %s %s v %s -> sofascore event %s',
                                date, home, away, event['id'])
                    return int(event['id'])
        logger.warning('no sofascore event for %s %s v %s', date, home, away)
        return None

    def match_xg(self, event_id: int) -> Dict[str, Optional[float]]:
        """Team xG and xGOT for one event, fetching only what it needs.

        WhoScored's feed carries no xG at all, so this is what fills the gap.
        When you already hold a bundle, use `xg_from_bundle` instead - same
        arithmetic, no second round of HTTP.
        """
        out = _shotmap_xg((self._endpoint(event_id, '/shotmap') or {}).get('shotmap') or [])
        if out['home_xg'] is None:
            stats = (self._endpoint(event_id, '/statistics') or {}).get('statistics') or []
            out.update(_statistics_xg(stats))
        return out


def _shotmap_xg(shotmap: List[dict]) -> Dict[str, Optional[float]]:
    """Sum per-shot xG/xGOT by side. The primary source."""
    out: Dict[str, Optional[float]] = {
        'home_xg': None, 'away_xg': None, 'home_xgot': None, 'away_xgot': None}
    totals = {True: [0.0, 0.0], False: [0.0, 0.0]}
    seen_xg = seen_xgot = False
    for shot in shotmap or []:
        side = bool(shot.get('isHome'))
        if shot.get('xg') is not None:
            totals[side][0] += float(shot['xg'])
            seen_xg = True
        if shot.get('xgot') is not None:
            totals[side][1] += float(shot['xgot'])
            seen_xgot = True

    if seen_xg:
        out['home_xg'] = round(totals[True][0], 2)
        out['away_xg'] = round(totals[False][0], 2)
    if seen_xgot:
        out['home_xgot'] = round(totals[True][1], 2)
        out['away_xgot'] = round(totals[False][1], 2)
    return out


def _statistics_xg(statistics: List[dict]) -> Dict[str, Optional[float]]:
    """Team-level 'Expected goals' from /statistics.

    The fallback when the shot map carries no xG, which is common outside the
    covered leagues. There is no xGOT equivalent here - it stays None.

    SofaScore often publishes literal 0.00 / 0.00 for competitions with no xG
    model (e.g. Community Shield). Treat that as missing rather than true zero.
    """
    out: Dict[str, Optional[float]] = {'home_xg': None, 'away_xg': None}
    for period in statistics or []:
        if period.get('period') != 'ALL':
            continue
        for group in period.get('groups', []):
            for item in group.get('statisticsItems', []):
                if item.get('name') == 'Expected goals':
                    home = _to_float(item.get('home'))
                    away = _to_float(item.get('away'))
                    if home == 0.0 and away == 0.0:
                        return out
                    out['home_xg'] = home
                    out['away_xg'] = away
    return out


def xg_from_bundle(bundle: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Team xG and xGOT out of an already-fetched bundle."""
    out = _shotmap_xg((bundle.get('shotmap') or {}).get('shotmap')
                      if isinstance(bundle.get('shotmap'), dict)
                      else bundle.get('shotmap'))
    if out['home_xg'] is None:
        stats = bundle.get('statistics')
        if isinstance(stats, dict):
            stats = stats.get('statistics')
        out.update(_statistics_xg(stats))
    return out


def bundle_teams(bundle: Dict[str, Any]) -> tuple:
    """(home, away) club names as SofaScore has them."""
    event = bundle.get('event') or {}
    return ((event.get('homeTeam') or {}).get('name'),
            (event.get('awayTeam') or {}).get('name'))


def _to_float(value):
    try:
        return round(float(str(value).strip().rstrip('%')), 2)
    except (TypeError, ValueError):
        return None


_NOISE = {'fc', 'afc', 'united', 'city', 'hove', 'albion', 'the', 'and'}

# Token overlap alone cannot decide the Premier League, in either direction.
#
# It says *no* where it should say yes: WhoScored abbreviates ('Man Utd',
# 'Spurs', 'Wolves') where SofaScore spells the club out, and those share no
# token at all - 8 of Arsenal's 38 league matches lost their xG to this.
#
# And it says *yes* where it must say no: 'united' and 'city' are stripped as
# noise so 'Manchester United' and 'Manchester City' both reduce to
# {'manchester'} and match each other. Home and away are positional throughout
# this pipeline, so that false positive is the more dangerous of the two - it
# writes a real club's xG onto the wrong match without a word.
#
# So the twenty clubs are named explicitly and compared by key. Anything not
# listed - Barcelona, Al Ahly, a cup opponent - falls through to the old token
# overlap, which is lenient but never worse than it was.
_CANONICAL = {
    'arsenal': 'arsenal',
    'aston villa': 'aston-villa',
    'bournemouth': 'bournemouth', 'afc bournemouth': 'bournemouth',
    'brentford': 'brentford',
    'brighton': 'brighton', 'brighton & hove albion': 'brighton',
    'brighton and hove albion': 'brighton', 'brighton hove albion': 'brighton',
    'burnley': 'burnley',
    'chelsea': 'chelsea',
    'coventry': 'coventry', 'coventry city': 'coventry',
    'crystal palace': 'crystal-palace',
    'everton': 'everton',
    'fulham': 'fulham',
    'hull': 'hull', 'hull city': 'hull',
    'ipswich': 'ipswich', 'ipswich town': 'ipswich',
    'leeds': 'leeds', 'leeds united': 'leeds',
    'leicester': 'leicester', 'leicester city': 'leicester',
    'liverpool': 'liverpool', 'liverpool fc': 'liverpool',
    'luton': 'luton', 'luton town': 'luton',
    'man city': 'man-city', 'manchester city': 'man-city',
    'man utd': 'man-utd', 'man united': 'man-utd',
    'manchester utd': 'man-utd', 'manchester united': 'man-utd',
    'newcastle': 'newcastle', 'newcastle united': 'newcastle',
    'nottingham forest': 'nottm-forest', 'nottm forest': 'nottm-forest',
    'sheffield united': 'sheffield-utd', 'sheffield utd': 'sheffield-utd',
    'southampton': 'southampton',
    'spurs': 'tottenham', 'tottenham': 'tottenham', 'tottenham hotspur': 'tottenham',
    'sunderland': 'sunderland',
    'west ham': 'west-ham', 'west ham united': 'west-ham',
    'wolves': 'wolves', 'wolverhampton': 'wolves',
    'wolverhampton wanderers': 'wolves',
}


def _clean(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Handles the typographic apostrophe in WhoScored's "Nott'm Forest" as well
    as the ascii one.
    """
    text = (name or '').lower()
    for ch in ('’', "'", '.', '-'):
        text = text.replace(ch, '')
    return ' '.join(text.split())


def _canonical(name: str) -> str | None:
    return _CANONICAL.get(_clean(name))


def _name_matches(wanted: str, actual: str) -> bool:
    left, right = _canonical(wanted), _canonical(actual)
    if left and right:
        return left == right

    def tokens(name):
        clean = (name or '').lower().replace('&', ' ').replace('.', ' ').replace('-', ' ')
        parts = [p for p in clean.split() if p not in _NOISE]
        return set(parts) or set(clean.split())
    return bool(tokens(wanted) & tokens(actual))


def names_match(wanted: str, actual: str) -> bool:
    """Public alias - the pipeline uses this to check the two feeds agree."""
    return _name_matches(wanted, actual)


def club_key(name: str) -> str | None:
    """One stable key per club, whichever feed's spelling you have.

    'Man Utd', 'Manchester Utd' and 'Manchester United' all give 'man-utd'.
    None for a club that is not in the Premier League table above. Anything
    keyed on a club - crests, colours - can use this instead of inventing its
    own spelling rules.
    """
    return _canonical(name)


def _walk_events(node: Any):
    """Yield every dict that looks like a SofaScore event object."""
    if isinstance(node, dict):
        if 'customId' in node and 'homeTeam' in node:
            yield node
        for value in node.values():
            yield from _walk_events(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_events(value)


def save_bundle(bundle: Dict[str, Any], path: str) -> str:
    with open(path, 'w') as fh:
        json.dump(bundle, fh)
    logger.info('saved sofascore bundle -> %s', path)
    return path


if __name__ == '__main__':
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description='Fetch a match from SofaScore')
    ap.add_argument('url_or_id')
    ap.add_argument('--out', required=True)
    ap.add_argument('--no-heatmaps', action='store_true')
    args = ap.parse_args()

    client = SofaScoreClient()
    target = args.url_or_id
    eid = int(target) if target.isdigit() else client.resolve_event_id(target)
    save_bundle(client.fetch_match(eid, with_heatmaps=not args.no_heatmaps), args.out)
