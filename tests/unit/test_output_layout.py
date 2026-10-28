"""Tests for the season output layout.

Regression cover for the `_latest -> _latest` bug: on the second render of a
season the symlink already resolved to a real match directory, so `_latest` was
picked up as a match of its own, won the (date, matchweek) tie on name order,
and became its own symlink target.
"""

import json
import os

import pytest

from visualizer import output_layout as layout


def _render(season_root, season, date, home, away, matchweek=None, panels=3,
            competition=None, club=None, job_id=None):
    """Simulate a render: create the match dir, drop panels, finalise."""
    match_path = layout.match_dir(season=season, date=date, home=home, away=away,
                                  matchweek=matchweek, competition=competition,
                                  output_root=season_root, club=club)
    panels_path = layout.panels_dir(match_path)
    for i in range(panels):
        open(os.path.join(panels_path, f'{i:02d}_panel.png'), 'wb').close()
    open(os.path.join(match_path, '00_match_dashboard_unified.png'), 'wb').close()
    layout.finalise(match_path, {
        'source': 'whoscored', 'season': season, 'matchweek': matchweek,
        'date': date, 'home_team': home, 'away_team': away, 'score': '1-2',
        'job_id': job_id,
    })
    return match_path


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / 'output')


def _index(root, season, club=None):
    with open(os.path.join(layout.season_root(season, output_root=root, club=club),
                           layout.INDEX_NAME)) as fh:
        return json.load(fh)


def _latest_path(root, season, club=None):
    return os.path.join(layout.season_root(season, output_root=root, club=club),
                        layout.LATEST_LINK)


# --- slugs and names --------------------------------------------------------

def test_slugify_handles_punctuation_and_accents():
    assert layout.slugify('Brighton & Hove Albion') == 'brighton-hove-albion'
    assert layout.slugify('Atlético Madrid') == 'atletico-madrid'
    assert layout.slugify('AFC Bournemouth') == 'afc-bournemouth'


def test_season_dir_name():
    assert layout.season_dir_name('2026/2027') == '2026-2027'
    assert layout.season_dir_name(None, 'Club Friendly Games').startswith('friendlies-')


def test_match_dir_name_sorts_in_fixture_order():
    names = [layout.match_dir_name('2026-08-21', 'Arsenal', 'Coventry City', mw)
             for mw in (1, 2, 10, 17)]
    assert names == sorted(names), 'zero-padded matchweek must keep ls order'


def test_friendly_uses_f_prefix():
    name = layout.match_dir_name('2026-08-01', 'Girona FC', 'Arsenal', None)
    assert name.startswith('f_')


# --- the regression ---------------------------------------------------------

def test_latest_is_not_self_referential_after_second_render(root):
    season = '2026/2027'
    first = _render(root, season, '2026-08-21', 'Arsenal', 'Coventry City', 1)
    _render(root, season, '2026-08-29', 'Aston Villa', 'Arsenal', 2)

    link = _latest_path(root, season)
    assert os.path.islink(link)
    resolved = os.path.realpath(link)
    assert resolved != os.path.realpath(os.path.dirname(link) + '/' + layout.LATEST_LINK) \
        or os.path.basename(resolved) != layout.LATEST_LINK
    assert os.path.basename(resolved) != layout.LATEST_LINK
    assert os.path.isfile(os.path.join(resolved, 'match.json'))
    assert first  # first render still on disk


def test_index_never_contains_the_symlink(root):
    season = '2026/2027'
    _render(root, season, '2026-08-21', 'Arsenal', 'Coventry City', 1)
    _render(root, season, '2026-08-29', 'Aston Villa', 'Arsenal', 2)
    _render(root, season, '2026-09-05', 'Arsenal', 'Chelsea', 3)

    dirs = [m['dir'] for m in _index(root, season)['matches']]
    assert layout.LATEST_LINK not in dirs
    assert len(dirs) == 3
    assert len(set(dirs)) == 3


def test_latest_points_at_the_newest_match(root):
    season = '2026/2027'
    _render(root, season, '2026-08-21', 'Arsenal', 'Coventry City', 1)
    _render(root, season, '2026-09-05', 'Arsenal', 'Chelsea', 3)
    _render(root, season, '2026-08-29', 'Aston Villa', 'Arsenal', 2)  # out of order

    newest = os.path.basename(os.path.realpath(_latest_path(root, season)))
    assert newest == layout.match_dir_name('2026-09-05', 'Arsenal', 'Chelsea', 3)


def test_index_sorted_newest_first(root):
    season = '2026/2027'
    for date, home, away, mw in (('2026-08-21', 'Arsenal', 'Coventry City', 1),
                                 ('2026-09-05', 'Arsenal', 'Chelsea', 3),
                                 ('2026-08-29', 'Aston Villa', 'Arsenal', 2)):
        _render(root, season, date, home, away, mw)
    dates = [m['date'] for m in _index(root, season)['matches']]
    assert dates == sorted(dates, reverse=True)


def test_repeated_render_is_idempotent(root):
    """Re-rendering the same match must not duplicate it in the index."""
    season = '2026/2027'
    for _ in range(3):
        _render(root, season, '2026-08-21', 'Arsenal', 'Coventry City', 1)
    matches = _index(root, season)['matches']
    assert len(matches) == 1
    assert matches[0]['panel_count'] == 3


def test_seasons_are_isolated(root):
    _render(root, '2026/2027', '2026-08-21', 'Arsenal', 'Coventry City', 1)
    _render(root, None, '2026-08-01', 'Girona FC', 'Arsenal',
            competition='Club Friendly Games')

    assert len(_index(root, '2026/2027')['matches']) == 1
    friendly_season = layout.season_dir_name(None, 'Club Friendly Games')
    with open(os.path.join(root, friendly_season, layout.INDEX_NAME)) as fh:
        assert len(json.load(fh)['matches']) == 1


# --- manifest ---------------------------------------------------------------

def test_manifest_records_panels_and_dashboard(root):
    match_path = _render(root, '2026/2027', '2026-08-21', 'Arsenal', 'Coventry City', 1, panels=18)
    with open(os.path.join(match_path, 'match.json')) as fh:
        manifest = json.load(fh)
    assert len(manifest['panels']) == 18
    assert manifest['dashboard'] == '00_match_dashboard_unified.png'
    assert manifest['rendered_at'].endswith('Z')


# --- club level -------------------------------------------------------------
#
# One schema per club in the warehouse, one directory per club on disk. Arsenal
# predates both and keeps the flat layout, which is what club=None must mean.

def test_club_none_is_the_legacy_flat_path(root):
    """The Arsenal DAG passes no club; its output must not move."""
    path = _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17)
    assert path == os.path.join(root, '2026-2027',
                                'mw17_2026-12-26_crystal-palace_v_arsenal')


def test_club_nests_one_level(root):
    path = _render(root, '2025/2026', '2025-11-26', 'Barcelona', 'Chelsea', 6,
                   club='barcelona')
    assert path == os.path.join(root, 'barcelona', '2025-2026',
                                'mw06_2025-11-26_barcelona_v_chelsea')


def test_club_slug_is_normalised(root):
    path = _render(root, '2025/2026', '2025-11-26', 'AC Milan', 'Inter', 12,
                   club='AC Milan')
    assert os.path.join(root, 'ac-milan', '2025-2026') in path


def test_each_club_gets_its_own_index_and_latest(root):
    _render(root, '2025/2026', '2025-11-26', 'Barcelona', 'Chelsea', 6, club='barcelona')
    _render(root, '2025/2026', '2025-11-27', 'Inter', 'AC Milan', 12, club='ac_milan')

    barca = _index(root, '2025/2026', club='barcelona')
    milan = _index(root, '2025/2026', club='ac_milan')
    assert len(barca['matches']) == 1
    assert len(milan['matches']) == 1
    assert barca['matches'][0]['home'] == 'Barcelona'
    assert milan['matches'][0]['home'] == 'Inter'

    assert os.path.islink(_latest_path(root, '2025/2026', club='barcelona'))
    assert os.path.islink(_latest_path(root, '2025/2026', club='ac_milan'))


def test_club_output_does_not_leak_into_the_flat_index(root):
    _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17)
    _render(root, '2026/2027', '2026-12-27', 'Barcelona', 'Chelsea', 6, club='barcelona')

    flat = _index(root, '2026/2027')
    assert [m['home'] for m in flat['matches']] == ['Crystal Palace']


def test_latest_self_reference_regression_holds_per_club(root):
    """The _latest -> _latest bug, re-checked one directory deeper."""
    for day, home in (('2025-11-26', 'Barcelona'), ('2025-12-03', 'Girona')):
        _render(root, '2025/2026', day, home, 'Chelsea', club='barcelona')

    link = _latest_path(root, '2025/2026', club='barcelona')
    assert os.path.islink(link)
    assert os.readlink(link) != layout.LATEST_LINK
    assert os.path.isfile(os.path.join(link, 'match.json'))


# --- finding a render again by job id ---------------------------------------
#
# Directory names come from the fixture, not the job, so match.json's job_id is
# the only way back. /visualizations/{job_id} depends on this.

def test_find_match_dir_by_job_id(root):
    path = _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17,
                   job_id='job-abc')
    assert layout.find_match_dir('job-abc', output_root=root) == path


def test_find_match_dir_works_under_a_club(root):
    path = _render(root, '2025/2026', '2025-11-26', 'Barcelona', 'Chelsea', 6,
                   club='barcelona', job_id='job-xyz')
    assert layout.find_match_dir('job-xyz', output_root=root) == path


def test_find_match_dir_picks_the_right_one_of_many(root):
    _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17, job_id='a')
    wanted = _render(root, '2026/2027', '2027-01-02', 'Arsenal', 'Everton', 18, job_id='b')
    _render(root, '2025/2026', '2025-11-26', 'Barcelona', 'Chelsea', 6,
            club='barcelona', job_id='c')
    assert layout.find_match_dir('b', output_root=root) == wanted


def test_find_match_dir_never_returns_the_latest_symlink(root):
    """_latest resolves to a real match dir, so a naive walk finds it twice."""
    for day in ('2026-12-26', '2027-01-02'):
        _render(root, '2026/2027', day, 'Arsenal', 'Everton', job_id=day)

    found = layout.find_match_dir('2027-01-02', output_root=root)
    assert os.path.basename(found) != layout.LATEST_LINK
    assert not os.path.islink(found)


def test_find_match_dir_returns_none_when_absent(root):
    _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17, job_id='a')
    assert layout.find_match_dir('nope', output_root=root) is None


def test_find_match_dir_on_a_missing_root(tmp_path):
    assert layout.find_match_dir('a', output_root=str(tmp_path / 'nothing')) is None


def test_find_match_dir_survives_a_corrupt_manifest(root):
    path = _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17,
                   job_id='good')
    broken = _render(root, '2026/2027', '2027-01-02', 'Arsenal', 'Everton', 18)
    with open(os.path.join(broken, 'match.json'), 'w') as fh:
        fh.write('{not json')
    assert layout.find_match_dir('good', output_root=root) == path


def test_match_panels_lists_dashboard_then_panels(root):
    path = _render(root, '2026/2027', '2026-12-26', 'Crystal Palace', 'Arsenal', 17,
                   panels=3, job_id='a')
    names = layout.match_panels(path)
    assert names[0] == '00_match_dashboard_unified.png'
    assert names[1:] == ['00_panel.png', '01_panel.png', '02_panel.png']
