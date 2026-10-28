"""Unit tests for WhoScored slug → match id resolution."""

import pytest

from scraper.fixtures import FixtureLookupError, resolve_from_html, resolve_match_id


SAMPLE = """
<a href="/matches/1983558/live/england-premier-league-2026-2027-aston-villa-arsenal">Villa</a>
<a href="/matches/1983568/show/england-premier-league-2026-2027-arsenal-chelsea">Chelsea</a>
<a href="/matches/1983540/live/england-premier-league-2026-2027-arsenal-coventry">Coventry</a>
"""


def test_exact_slug_match():
    assert resolve_from_html(SAMPLE, 'england-premier-league-2026-2027-aston-villa-arsenal') == 1983558
    assert resolve_from_html(SAMPLE, 'england-premier-league-2026-2027-arsenal-chelsea') == 1983568


def test_missing_slug_returns_none():
    assert resolve_from_html(SAMPLE, 'england-premier-league-2026-2027-arsenal-liverpool') is None


def test_loose_club_token_match():
    # WhoScored sometimes drops "united" / similar tokens; tokens subset still matches.
    html = '<a href="/matches/99/live/england-premier-league-2026-2027-arsenal-newcastle">x</a>'
    assert resolve_from_html(html, 'england-premier-league-2026-2027-arsenal-newcastle-united') == 99


# --- resolve_match_id: "could not look" is not "not there" -------------------

class _Scraper:
    """Stands in for WhoscoredScraper. `pages` maps url substring -> html."""

    def __init__(self, pages=None, raise_all=False):
        self.pages = pages or {}
        self.raise_all = raise_all
        self.fetched = []
        self.closed = False

    def fetch_html(self, url, wait_time=8):
        self.fetched.append(url)
        if self.raise_all:
            raise RuntimeError('403 bot wall')
        for needle, html in self.pages.items():
            if needle in url:
                return html
        return ''

    def close(self):
        self.closed = True


def test_every_source_failing_raises_rather_than_returning_none():
    """A bot wall used to be indistinguishable from an unpublished fixture."""
    scraper = _Scraper(raise_all=True)
    with pytest.raises(FixtureLookupError):
        resolve_match_id('england-premier-league-2026-2027-sunderland-arsenal',
                         scraper=scraper)


def test_fetched_cleanly_but_absent_returns_none():
    scraper = _Scraper(pages={'whoscored.com': SAMPLE})
    assert resolve_match_id('england-premier-league-2026-2027-sunderland-arsenal',
                            scraper=scraper) is None


def test_resolves_from_the_team_page_without_touching_the_grid():
    html = ('<a href="/matches/1983567/live/'
            'england-premier-league-2026-2027-sunderland-arsenal">x</a>')
    scraper = _Scraper(pages={'/teams/13/fixtures/': html})
    assert resolve_match_id('england-premier-league-2026-2027-sunderland-arsenal',
                            scraper=scraper) == 1983567
    assert len(scraper.fetched) == 1


def test_falls_back_to_the_discovered_grid_when_the_pinned_one_is_stale():
    """The pinned seasons/stages ids roll every August; discovery survives it."""
    live_grid = ('/regions/252/tournaments/2/seasons/99999/stages/88888'
                 '/fixtures/england-premier-league-2026-2027')
    scraper = _Scraper(pages={
        'seasons/99999': ('<a href="/matches/1983567/live/'
                          'england-premier-league-2026-2027-sunderland-arsenal">x</a>'),
        'tournaments/2': f'<a href="{live_grid}">fixtures</a>',
    })
    assert resolve_match_id('england-premier-league-2026-2027-sunderland-arsenal',
                            scraper=scraper) == 1983567


def test_a_caller_supplied_scraper_is_not_closed():
    scraper = _Scraper(pages={'whoscored.com': SAMPLE})
    resolve_match_id('england-premier-league-2026-2027-sunderland-arsenal',
                     scraper=scraper)
    assert not scraper.closed
