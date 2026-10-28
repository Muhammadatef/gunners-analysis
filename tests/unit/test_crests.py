"""Club crests: the file has to be what its name says, and the lookup has to
find it from whichever feed's spelling of the club we happen to hold.

Both of these failed in the 2025/26 backfill. Five assets were WebP with a
.png extension, and matplotlib picks its decoder from the extension - so
Chelsea v Arsenal died at the render stage with "not a PNG file" after the
scrape, transform and xG enrichment had all succeeded. Separately, the filename
was derived from the team name, so WhoScored's short names ('Newcastle', not
'Newcastle United') quietly found no crest at all.
"""

import glob
import os

import pytest

from visualizer.match_report import ASSETS, load_logo


def _assets():
    return sorted(glob.glob(os.path.join(ASSETS, '*.png')))


def test_there_are_crests_to_check():
    assert _assets(), f'no crest assets found under {ASSETS}'


@pytest.mark.parametrize('path', _assets(), ids=os.path.basename)
def test_every_png_asset_really_is_a_png(path):
    """A .png that is secretly WebP reads fine in a browser and not here."""
    from PIL import Image

    with Image.open(path) as im:
        assert im.format == 'PNG', f'{os.path.basename(path)} is {im.format}'


# WhoScored abbreviates; SofaScore and logos.json spell out. Either must work.
@pytest.mark.parametrize('name', [
    'Arsenal',
    'Man Utd', 'Manchester United',
    'Man City', 'Manchester City',
    'Spurs', 'Tottenham Hotspur',
    'Newcastle', 'Newcastle United',
    'Brighton', 'Brighton & Hove Albion',
    'Bournemouth', 'AFC Bournemouth',
    "Nott'm Forest", 'Nottingham Forest',
    'Leeds', 'Leeds United',
    'Chelsea', 'Liverpool', 'Crystal Palace', 'Everton', 'Fulham',
    'Aston Villa', 'Brentford', 'Sunderland', 'Ipswich',
    # the 2026/27 promoted clubs - MW1 is Arsenal v Coventry
    'Coventry', 'Coventry City', 'Hull', 'Hull City',
    # fetched after the first backfill drew these three with no crest at all
    'Burnley', 'West Ham', 'West Ham United',
    'Wolves', 'Wolverhampton',
])
def test_a_crest_resolves_from_either_spelling(name):
    assert load_logo(name) is not None, f'no crest for {name!r}'


def test_an_unknown_club_is_none_not_an_error():
    assert load_logo('Real Madrid') is None


def test_every_2025_26_opponent_has_a_crest():
    """The whole field, as WhoScored spells it."""
    field = ['Arsenal', 'Man Utd', 'Leeds', 'Liverpool', "Nott'm Forest",
             'Man City', 'Newcastle', 'West Ham', 'Fulham', 'Crystal Palace',
             'Burnley', 'Sunderland', 'Spurs', 'Chelsea', 'Brentford',
             'Aston Villa', 'Wolves', 'Bournemouth', 'Brighton', 'Everton']
    missing = [n for n in field if load_logo(n) is None]
    assert not missing, f'no crest for {missing}'
