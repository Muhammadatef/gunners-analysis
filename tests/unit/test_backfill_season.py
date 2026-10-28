"""Backfilling a completed season: what gets run, what gets skipped, what counts as done.

No network and no browser here - the parts worth testing are the selection
logic and the two post-load assertions, because those are what decide whether a
two-hour run can be trusted or has to be done again.
"""

import json

import pytest

from orchestrator import backfill_season as bf


def _fixture(matchweek, match_id, **over):
    base = {
        'matchweek': matchweek,
        'date': f'2025-08-{matchweek:02d}',
        'kickoff_uk': '15:00',
        'kickoff_confirmed': True,
        'opponent': 'Someone',
        'venue': 'H',
        'home_team': 'Arsenal',
        'away_team': 'Someone',
        'whoscored_slug': f'england-premier-league-2025-2026-arsenal-someone-{matchweek}',
        'fixture_key': f'gw{matchweek:02d}_2025-08-{matchweek:02d}_arsenal_v_someone',
        'sofascore_event_id': 900000 + matchweek,
        'final_score': '1-0',
        'finished': True,
        'whoscored_match_id': match_id,
    }
    base.update(over)
    return base


def _calendar(*fixtures):
    return {'season': '2025/2026', 'competition': 'Premier League',
            'team': 'Arsenal', 'fixtures': list(fixtures)}


# --- matchweek selection ----------------------------------------------------

@pytest.mark.parametrize('spec,expected', [
    (None, None),
    ('1', {1}),
    ('1,3,5', {1, 3, 5}),
    ('5-9', {5, 6, 7, 8, 9}),
    ('1,4-6,38', {1, 4, 5, 6, 38}),
    ('  2 , 3  ', {2, 3}),
    ('', None),
])
def test_parse_only(spec, expected):
    assert bf.parse_only(spec) == expected


# --- what runs and what does not --------------------------------------------

def test_matches_already_in_the_warehouse_are_skipped():
    calendar = _calendar(_fixture(1, 111), _fixture(2, 222))
    todo, skipped = bf.select(calendar, done={111}, only=None, force=False)

    assert [f['matchweek'] for f in todo] == [2]
    assert [(s['matchweek'], s['why']) for s in skipped] == [(1, 'already in the warehouse')]


def test_force_re_ingests_what_is_already_there():
    calendar = _calendar(_fixture(1, 111), _fixture(2, 222))
    todo, skipped = bf.select(calendar, done={111, 222}, only=None, force=True)

    assert [f['matchweek'] for f in todo] == [1, 2]
    assert skipped == []


def test_unfinished_fixtures_are_never_ingested():
    """A season still in progress must not have its future scraped."""
    calendar = _calendar(_fixture(1, 111), _fixture(2, 222, finished=False))
    todo, skipped = bf.select(calendar, done=set(), only=None, force=False)

    assert [f['matchweek'] for f in todo] == [1]
    assert [(s['matchweek'], s['why']) for s in skipped] == [(2, 'not finished')]


def test_force_does_not_override_unfinished():
    calendar = _calendar(_fixture(1, 111, finished=False))
    todo, _ = bf.select(calendar, done=set(), only=None, force=True)
    assert todo == []


def test_only_restricts_to_the_given_matchweeks():
    calendar = _calendar(_fixture(1, 111), _fixture(2, 222), _fixture(3, 333))
    todo, skipped = bf.select(calendar, done=set(), only={2}, force=False)

    assert [f['matchweek'] for f in todo] == [2]
    # A matchweek filtered out is not "skipped" - it was never in scope.
    assert skipped == []


# --- the xG/xGOT gate -------------------------------------------------------

def test_xg_is_complete_only_with_both_halves_from_sofascore():
    assert bf.xg_complete({'xg': '1.5-1.3', 'xgot': '0.7-1.2',
                           'xg_source': 'sofascore'}) is True


def test_sides_swapped_still_counts_as_sofascore():
    assert bf.xg_complete({'xg': '1.5-1.3', 'xgot': '0.7-1.2',
                           'xg_source': 'sofascore (sides swapped)'}) is True


def test_fotmob_source_counts_as_complete():
    assert bf.xg_complete({'xg': '1.88-0.20', 'xgot': '2.44-0.04',
                           'xg_source': 'fotmob'}) is True


@pytest.mark.parametrize('stored', [
    {'xg': None, 'xgot': '0.7-1.2', 'xg_source': 'sofascore'},
    {'xg': '1.5-1.3', 'xgot': None, 'xg_source': 'sofascore'},
    {'xg': '1.5-1.3', 'xgot': '0.7-1.2', 'xg_source': None},
    {'xg': '1.5-1.3', 'xgot': '0.7-1.2',
     'xg_source': 'unavailable: sofascore teams do not match'},
])
def test_incomplete_xg_is_rejected(stored):
    """WhoScored carries no xG at all, so anything but SofaScore is a placeholder."""
    assert bf.xg_complete(stored) is False


# --- reading the stored row back --------------------------------------------

class _Cur:
    def __init__(self, row):
        self.row = row

    def execute(self, *a):
        pass

    def fetchone(self):
        return self.row


@pytest.fixture
def stub_cursor(monkeypatch):
    """Hand stored_match() one row without going near Postgres."""
    import contextlib

    def install(row):
        @contextlib.contextmanager
        def fake_cursor(*a, **k):
            yield _Cur(row)

        @contextlib.contextmanager
        def fake_use_schema(*a, **k):
            yield

        monkeypatch.setattr(bf, 'cursor', fake_cursor)
        monkeypatch.setattr(bf, 'use_schema', fake_use_schema)
    return install


def test_a_zero_xgot_is_a_real_value(stub_cursor):
    """A side with no shots on target has 0.0 xGOT - not a missing one."""
    stub_cursor((2, 0, 2.91, 0.43, 2.20, 0.0, 'sofascore'))
    stored = bf.stored_match('gunners', 1)

    assert stored['xgot'] == '2.2-0.0'
    assert bf.xg_complete(stored) is True


def test_half_a_pair_counts_as_missing(stub_cursor):
    """One side null is as broken as both, and must not read as complete."""
    stub_cursor((2, 0, 2.91, 0.43, 2.20, None, 'sofascore'))
    stored = bf.stored_match('gunners', 1)

    assert stored['xgot'] is None
    assert bf.xg_complete(stored) is False


def test_absent_match_reads_as_none(stub_cursor):
    stub_cursor(None)
    assert bf.stored_match('gunners', 1) is None


# --- refusing to start a long run on a broken calendar ----------------------

def test_load_calendar_rejects_a_missing_whoscored_id(tmp_path):
    path = tmp_path / 'cal.json'
    path.write_text(json.dumps(_calendar(_fixture(1, 111),
                                         _fixture(2, None))))

    with pytest.raises(SystemExit) as exc:
        bf.load_calendar('2025/2026', str(path))
    assert 'whoscored_match_id' in str(exc.value)
    assert 'gw02' in str(exc.value)


def test_load_calendar_rejects_a_missing_sofascore_id(tmp_path):
    path = tmp_path / 'cal.json'
    path.write_text(json.dumps(_calendar(_fixture(1, 111, sofascore_event_id=None))))

    with pytest.raises(SystemExit) as exc:
        bf.load_calendar('2025/2026', str(path))
    assert 'sofascore_event_id' in str(exc.value)


def test_load_calendar_says_how_to_build_a_missing_one(tmp_path):
    with pytest.raises(SystemExit) as exc:
        bf.load_calendar('2025/2026', str(tmp_path / 'nope.json'))
    assert 'build_season_calendar.py' in str(exc.value)


def test_load_calendar_accepts_a_complete_one(tmp_path):
    path = tmp_path / 'cal.json'
    path.write_text(json.dumps(_calendar(_fixture(1, 111), _fixture(2, 222))))

    data = bf.load_calendar('2025/2026', str(path))
    assert len(data['fixtures']) == 2


# --- the URLs the calendar's pre-resolved ids turn into ---------------------

def test_sofascore_url_carries_the_event_id_as_a_fragment():
    """resolve_event_id short-circuits on '#id:', so this costs no HTTP."""
    from scraper.sofascore import SofaScoreClient

    url = bf.SOFASCORE_URL.format(event_id=14025056)
    assert SofaScoreClient().resolve_event_id(url) == 14025056
