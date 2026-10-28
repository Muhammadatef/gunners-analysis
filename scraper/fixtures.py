"""Resolve a WhoScored match id for a scheduled fixture.

WhoScored only mints a match id a few weeks before kick-off, so the season
calendar stores a slug (`england-premier-league-2026-2027-arsenal-chelsea`) and
this module turns it into a real URL when the time comes.

Strategies, tried in order:

1. Arsenal's WhoScored fixtures page - every match links to /matches/<id>/...
   This is one page load for the whole season, so it is the cheap path.
2. The Premier League tournament fixtures page for the current season stage
   (covers the full grid, not just Arsenal's team page).

Both use the existing Selenium scraper so we inherit its anti-bot setup.

Override the PL fixtures URL when WhoScored rolls a new season/stage id:

    GUNNERS_WHOSCORED_PL_FIXTURES_URL=https://www.whoscored.com/regions/.../fixtures/...
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

class FixtureLookupError(RuntimeError):
    """Every source failed to load. Distinct from 'the fixture is not there'."""


ARSENAL_TEAM_ID = 13
TEAM_FIXTURES_URL = 'https://www.whoscored.com/teams/{team_id}/fixtures/'
MATCH_URL = 'https://www.whoscored.com/matches/{match_id}/live/{slug}'

# The competition landing page redirects to whatever season/stage is current,
# so the grid can be discovered instead of pinned to ids that roll every year.
PL_TOURNAMENT_URL = 'https://www.whoscored.com/regions/252/tournaments/2'
# /regions/252/tournaments/2/seasons/11141/stages/25544/fixtures/...
STAGE_HREF_RE = re.compile(
    r'/regions/\d+/tournaments/\d+/seasons/\d+/stages/\d+/fixtures/[a-z0-9\-]+')

# 2026/27 Premier League stage fixtures grid (WhoScored).
DEFAULT_PL_FIXTURES_URL = (
    'https://www.whoscored.com/regions/252/tournaments/2/seasons/11141'
    '/stages/25544/fixtures/england-premier-league-2026-2027'
)

# /matches/1903353/live/... or /matches/1983558/show/...
MATCH_HREF_RE = re.compile(r'/matches/(\d+)/(?:live|show)/([a-z0-9\-]+)')


def pl_fixtures_url() -> str:
    # compose passes the variable through as "" when it is unset, and an empty
    # string is not a URL - os.getenv's default would never fire on it.
    return os.getenv('GUNNERS_WHOSCORED_PL_FIXTURES_URL', '').strip() or DEFAULT_PL_FIXTURES_URL


def discover_pl_fixtures_url(scraper) -> str | None:
    """Find the live PL fixtures grid from the tournament landing page.

    DEFAULT_PL_FIXTURES_URL freezes `seasons/11141/stages/25544`. WhoScored
    mints new ones every August, and when they roll, the hardcoded URL returns
    a page with no hrefs at all - which the resolver cannot tell apart from a
    fixture that simply is not published yet. Reading the ids off the landing
    page keeps the fallback alive across a season boundary.
    """
    try:
        html = scraper.fetch_html(PL_TOURNAMENT_URL)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning('could not fetch %s: %s', PL_TOURNAMENT_URL, exc)
        return None
    found = STAGE_HREF_RE.search(html)
    if not found:
        logger.warning('no stage fixtures link on %s', PL_TOURNAMENT_URL)
        return None
    url = f'https://www.whoscored.com{found.group(0)}'
    logger.info('discovered PL fixtures grid: %s', url)
    return url


def _hrefs_from_html(html: str) -> list[tuple[int, str]]:
    return [(int(mid), slug) for mid, slug in MATCH_HREF_RE.findall(html)]


def resolve_from_html(html: str, slug: str) -> int | None:
    """Find the match id whose slug matches the fixture we want.

    Falls back to a season + both-club-tokens match, so a slug spelling change
    on WhoScored's side (they use `newcastle`, not `newcastle-united`) degrades
    to a near-miss lookup instead of silently returning nothing.
    """
    hrefs = _hrefs_from_html(html)

    for match_id, found_slug in hrefs:
        if found_slug == slug:
            return match_id

    season_prefix = 'england-premier-league-'
    if season_prefix in slug:
        # Keep competition + season year tokens, e.g. england-premier-league-2026-2027
        parts = slug.split('-')
        # england premier league YYYY YYYY ...
        season_prefix = '-'.join(parts[:5]) if len(parts) >= 5 else slug[:40]
    else:
        season_prefix = slug[:40]
    wanted = _club_tokens(slug)
    for match_id, found_slug in hrefs:
        if not found_slug.startswith(season_prefix):
            continue
        found = _club_tokens(found_slug)
        if wanted and found and (wanted <= found or found <= wanted):
            logger.warning('slug %s matched loosely against %s', slug, found_slug)
            return match_id
    return None


def _club_tokens(slug: str) -> set[str]:
    """Distinctive words from a slug, ignoring the competition/season prefix."""
    stop = {'england', 'premier', 'league', 'united', 'city', 'fc', 'afc', 'hove', 'albion'}
    parts = [p for p in slug.split('-') if not p.isdigit()]
    return {p for p in parts if p not in stop}


def resolve_match_id(slug: str, scraper=None, team_id: int = ARSENAL_TEAM_ID) -> int | None:
    """Return the WhoScored match id for `slug`, or None if not published yet.

    Raises FixtureLookupError if no source could be loaded at all. The two
    outcomes used to be the same `None`, which meant a bot wall, a dead Chrome
    and a genuinely unpublished fixture were indistinguishable to the caller -
    and the season DAG turned all three into "WhoScored has not published a
    match id yet" and skipped. Only the last of those is worth being quiet
    about.
    """
    from scraper.scraper import WhoscoredScraper

    own = scraper is None
    scraper = scraper or WhoscoredScraper()
    try:
        sources = [
            ('team fixtures', TEAM_FIXTURES_URL.format(team_id=team_id)),
            ('PL fixtures', pl_fixtures_url()),
        ]
        fetched = 0
        for label, url in sources:
            try:
                html = scraper.fetch_html(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning('could not fetch %s (%s): %s', label, url, exc)
                continue
            fetched += 1
            match_id = resolve_from_html(html, slug)
            if match_id:
                logger.info('resolved %s -> match %s via %s', slug, match_id, label)
                return match_id
            logger.info('slug %s not found on %s yet', slug, label)

        # The pinned grid may simply have aged out; ask the site where it is now.
        discovered = discover_pl_fixtures_url(scraper)
        if discovered and discovered != pl_fixtures_url():
            try:
                html = scraper.fetch_html(discovered)
            except Exception as exc:                             # noqa: BLE001
                logger.warning('could not fetch discovered grid %s: %s', discovered, exc)
            else:
                fetched += 1
                match_id = resolve_from_html(html, slug)
                if match_id:
                    logger.info('resolved %s -> match %s via discovered grid',
                                slug, match_id)
                    return match_id
                logger.info('slug %s not found on the discovered grid either', slug)

        if not fetched:
            raise FixtureLookupError(
                f'could not load any WhoScored fixture listing for {slug}; '
                f'this is a lookup failure, not an unpublished fixture')
        return None
    finally:
        if own:
            scraper.close()


def match_url(match_id: int, slug: str) -> str:
    return MATCH_URL.format(match_id=match_id, slug=slug)


def is_probably_finished(kickoff: datetime, now: datetime | None = None,
                         minutes: int = 135) -> bool:
    """A PL match plus stoppage plus WhoScored's own processing lag."""
    now = now or datetime.utcnow()
    return (now - kickoff).total_seconds() >= minutes * 60
