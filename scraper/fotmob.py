"""FotMob client - xG/xGOT fallback when SofaScore's API is blocked.

SofaScore's public JSON endpoints often return 403 from datacenter / Docker
egress. FotMob's `/api/data/*` routes still answer with match xG and xGOT for
Premier League games, which is what `enrich_xg` needs after WhoScored.

It also answers a question no other source here can without a browser: has the
match actually finished, and when did it really kick off. The season calendar
only guesses both.

    from scraper.fotmob import FotMobClient
    c = FotMobClient()
    mid = c.find_match('2026-08-21', 'Arsenal', 'Coventry')
    xg = c.match_xg(mid)
    c.match_status('2026-09-12', 'Sunderland', 'Arsenal')['finished']
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

API = 'https://www.fotmob.com/api/data'
WEB = 'https://www.fotmob.com'

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# Titles FotMob uses in content.stats for the numbers we want.
_XG_TITLES = {'Expected goals (xG)', 'Expected goals'}
_XGOT_TITLES = {'xG on target (xGOT)', 'Expected goals on target (xGOT)'}


# Age-group, reserve and women's sides FotMob spells into the team or league
# name. Anything here that the caller did not ask for marks a wrong squad.
_SQUAD_MARKERS = {
    'u15', 'u16', 'u17', 'u18', 'u19', 'u20', 'u21', 'u22', 'u23',
    'women', 'womens', 'w', 'ladies', 'reserves', 'academy', 'youth', 'ii',
}


def _norm(value: Optional[str]) -> str:
    return (value or '').strip().casefold()


def _squad_markers(name: Optional[str]) -> set:
    """Squad qualifiers present in a team or league name."""
    tokens = re.split(r'[^a-z0-9]+', _norm(name))
    return {t for t in tokens if t in _SQUAD_MARKERS}


class FotMobError(RuntimeError):
    pass


class FotMobClient:
    """Thin client over FotMob's public match JSON."""

    def __init__(self, timeout: int = 25, retries: int = 3, pause: float = 0.2):
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

    def _get(self, url: str) -> Any:
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                time.sleep(self.pause)
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.warning('fotmob GET %s failed (%d/%d): %s',
                               url, attempt, self.retries, exc)
                time.sleep(1.5 * attempt)
        raise FotMobError(f'GET {url} failed after {self.retries} attempts: {last}')

    def matches_on(self, date: str) -> List[dict]:
        """All fixtures FotMob lists for `YYYY-MM-DD` (UTC calendar day)."""
        ymd = date.replace('-', '')
        data = self._get(f'{API}/matches?date={ymd}') or {}
        out: List[dict] = []
        for league in data.get('leagues') or []:
            for match in league.get('matches') or []:
                match = dict(match)
                match['_league'] = league.get('name')
                out.append(match)
        return out

    def find_entry(self, date: str, home: str, away: str, *,
                   league: Optional[str] = None) -> Optional[dict]:
        """The raw listing entry for a fixture on `date`, or None.

        `names_match` is token overlap, so 'Arsenal' also matches 'Arsenal
        U21' - and on 12 Sep 2026 FotMob listed Arsenal U21 v Chelsea U21 in
        Premier League 2 on a day the senior sides did not play each other at
        all. Taking the first name hit is how an academy scoreline quietly
        becomes a senior xG figure, so entries carrying a squad qualifier the
        caller did not ask for are rejected outright, not merely ranked below
        the senior one. What survives is then ordered by league.
        """
        from scraper.sofascore import names_match

        candidates = []
        for match in self.matches_on(date):
            mh = (match.get('home') or {}).get('name') or ''
            ma = (match.get('away') or {}).get('name') or ''
            if not (names_match(home, mh) and names_match(away, ma)):
                continue
            spurious = ((_squad_markers(mh) - _squad_markers(home))
                        | (_squad_markers(ma) - _squad_markers(away))
                        | (_squad_markers(match.get('_league')) - _squad_markers(league)))
            if spurious:
                logger.info('skipping fotmob %s (%s %s v %s): %s squad, not asked for',
                            match['id'], match.get('_league'), mh, ma,
                            '/'.join(sorted(spurious)))
                continue
            candidates.append(match)

        if not candidates:
            return None
        if len(candidates) > 1:
            candidates.sort(key=lambda m: _norm(m.get('_league')) != _norm(league))
            logger.warning(
                'fotmob listed %d senior matches for %s %s v %s; picked %s (%s)',
                len(candidates), date, home, away, candidates[0]['id'],
                candidates[0].get('_league'))
        return candidates[0]

    def find_match(self, date: str, home: str, away: str, *,
                   league: Optional[str] = None) -> Optional[int]:
        """Locate a match id by date + home/away names."""
        match = self.find_entry(date, home, away, league=league)
        if match is None:
            logger.warning('no fotmob match for %s %s v %s', date, home, away)
            return None
        logger.info('matched %s %s v %s -> fotmob %s', date, home, away, match['id'])
        return int(match['id'])

    def match_status(self, date: str, home: str, away: str, *,
                     league: Optional[str] = None) -> Optional[dict]:
        """Whether a fixture has actually finished, and when it really started.

        The season calendar only guesses kick-off - 32 of the 38 2026/27
        fixtures have no confirmed time, so the DAG assumes 15:00 and can fire
        hours before the players come out. This is the cheap second opinion:
        FotMob answers unauthenticated with the real kick-off and a definitive
        full-time flag, so the pipeline can wait for the whistle instead of
        trusting the calendar.

        Returns None when the fixture is not listed for that date at all -
        which is itself informative: the match moved.
        """
        match = self.find_entry(date, home, away, league=league)
        if match is None:
            return None
        status = match.get('status') or {}
        return {
            'fotmob_id': int(match['id']),
            'finished': bool(status.get('finished')),
            'started': bool(status.get('started')),
            'cancelled': bool(status.get('cancelled')),
            'utc_kickoff': status.get('utcTime'),
            'score': status.get('scoreStr'),
            'reason': (status.get('reason') or {}).get('long'),
        }

    def fetch_match(self, match_id: int) -> Dict[str, Any]:
        data = self._get(f'{API}/matchDetails?matchId={match_id}')
        if not data:
            raise FotMobError(f'fotmob match {match_id} not found')
        return data

    def match_xg(self, match_id: int) -> Dict[str, Optional[float]]:
        return xg_from_details(self.fetch_match(match_id))

    def teams(self, details: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        return teams_from_details(details)


def teams_from_details(details: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    general = details.get('general') or {}
    header = details.get('header') or {}
    teams = header.get('teams') or []
    if len(teams) >= 2:
        return teams[0].get('name'), teams[1].get('name')
    home = (general.get('homeTeam') or {}).get('name')
    away = (general.get('awayTeam') or {}).get('name')
    return home, away


def _stat_pair(details: Dict[str, Any], titles: set[str]) -> Tuple[Optional[float], Optional[float]]:
    periods = (((details.get('content') or {}).get('stats') or {}).get('Periods') or {})
    all_stats = (periods.get('All') or {}).get('stats') or []
    for group in all_stats:
        for row in group.get('stats') or []:
            title = row.get('title') or row.get('key') or ''
            if title not in titles:
                continue
            vals = row.get('stats') or [None, None]
            if len(vals) < 2 or vals[0] is None or vals[1] is None:
                continue
            try:
                home = round(float(vals[0]), 2)
                away = round(float(vals[1]), 2)
            except (TypeError, ValueError):
                continue
            return home, away
    return None, None


def xg_from_details(details: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Team xG and xGOT from a matchDetails payload."""
    home_xg, away_xg = _stat_pair(details, _XG_TITLES)
    home_xgot, away_xgot = _stat_pair(details, _XGOT_TITLES)

    # Shot-map fallback when the summary row is missing.
    if home_xg is None:
        home_xg, away_xg, home_xgot, away_xgot = _shotmap_xg(details)

    return {
        'home_xg': home_xg,
        'away_xg': away_xg,
        'home_xgot': home_xgot,
        'away_xgot': away_xgot,
    }


def _shotmap_xg(details: Dict[str, Any]) -> Tuple[
        Optional[float], Optional[float], Optional[float], Optional[float]]:
    shots = (((details.get('content') or {}).get('shotmap') or {}).get('shots') or [])
    if not shots:
        return None, None, None, None

    general = details.get('general') or {}
    home_id = (general.get('homeTeam') or {}).get('id')
    totals = {True: [0.0, 0.0], False: [0.0, 0.0]}
    seen_xg = {True: False, False: False}
    seen_xgot = {True: False, False: False}
    for shot in shots:
        is_home = shot.get('teamId') == home_id
        if shot.get('expectedGoals') is not None:
            totals[is_home][0] += float(shot['expectedGoals'])
            seen_xg[is_home] = True
        if shot.get('expectedGoalsOnTarget') is not None:
            totals[is_home][1] += float(shot['expectedGoalsOnTarget'])
            seen_xgot[is_home] = True

    if not any(seen_xg.values()):
        return None, None, None, None

    home_xg = round(totals[True][0], 2)
    away_xg = round(totals[False][0], 2)
    home_xgot = round(totals[True][1], 2) if seen_xgot[True] else None
    away_xgot = round(totals[False][1], 2) if seen_xgot[False] else None
    return home_xg, away_xg, home_xgot, away_xgot
