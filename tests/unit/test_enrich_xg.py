"""Pulling xG out of SofaScore and into a WhoScored job.

WhoScored's feed carries no xG at all, so this is the only path to a real shot
map. The load-bearing part is the sides check: home/away identity is positional
everywhere in this pipeline, and nothing else cross-references the two feeds.
"""

import json
import os

import pytest

from orchestrator import pipeline
from scraper import sofascore


# --- fixtures ---------------------------------------------------------------

def _bundle(home='Barcelona', away='Chelsea', shots=((True, 0.6, 0.4), (False, 0.2, 0.1))):
    return {
        'source': 'sofascore',
        'event_id': 14025519,
        'event': {'homeTeam': {'name': home}, 'awayTeam': {'name': away}},
        'shotmap': {'shotmap': [
            {'isHome': is_home, 'xg': xg, 'xgot': xgot} for is_home, xg, xgot in shots]},
    }


class FakeClient:
    """Stands in for SofaScoreClient - no network, records what was called."""

    def __init__(self, bundle=None, event_id=14025519, raises=None):
        self.bundle = bundle if bundle is not None else _bundle()
        self.event_id = event_id
        self.raises = raises
        self.resolved_urls = []
        self.searched = False

    def resolve_event_id(self, url):
        if self.raises:
            raise self.raises
        self.resolved_urls.append(url)
        return self.event_id

    def find_event(self, date, home, away, **kwargs):
        self.searched = True
        return self.event_id

    def fetch_match(self, event_id, with_heatmaps=True):
        if self.raises:
            raise self.raises
        return self.bundle


@pytest.fixture
def job(tmp_root, monkeypatch):
    """A processed job with metadata but no xG, ready to enrich."""
    job_id = 'job-1'
    out = os.path.join(pipeline.PROCESSED_DIR, job_id)
    os.makedirs(out)
    with open(os.path.join(out, 'metadata.json'), 'w') as fh:
        json.dump({'matchId': '123', 'date': '2025-11-26',
                   'teams': {'2817': 'Barcelona', '15': 'Chelsea'},
                   'home_xg': 0.0, 'away_xg': 0.0}, fh)
    return job_id


def _meta(job_id):
    with open(os.path.join(pipeline.PROCESSED_DIR, job_id, 'metadata.json')) as fh:
        return json.load(fh)


def _install(monkeypatch, client):
    monkeypatch.setattr(sofascore, 'SofaScoreClient', lambda *a, **k: client)


class FakeFotMob:
    """No-network FotMob stub — miss by default so SofaScore failures stay visible."""

    def __init__(self, raises=None, match_id=None):
        self.raises = raises
        self.match_id = match_id

    def find_match(self, *a, **k):
        if self.raises:
            raise self.raises
        return self.match_id

    def fetch_match(self, *a, **k):
        if self.raises:
            raise self.raises
        raise RuntimeError('fotmob stub has no match payload')


def _block_fotmob(monkeypatch, raises=None):
    from scraper import fotmob
    monkeypatch.setattr(fotmob, 'FotMobClient',
                        lambda *a, **k: FakeFotMob(raises=raises))


# --- the happy path ---------------------------------------------------------

def test_url_resolves_and_writes_xg(job, monkeypatch):
    client = FakeClient()
    _install(monkeypatch, client)

    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')

    assert client.resolved_urls == ['https://www.sofascore.com/x/y']
    assert meta['home_xg'] == 0.6
    assert meta['away_xg'] == 0.2
    assert meta['xg_source'] == 'sofascore'
    assert meta['sofascore_event_id'] == 14025519
    assert _meta(job)['home_xg'] == 0.6          # and it was persisted


def test_bundle_is_saved_next_to_the_scrape(job, monkeypatch):
    _install(monkeypatch, FakeClient())
    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')

    path = os.path.join(pipeline.RAW_DIR, f'sofascore_{job}.json')
    assert os.path.isfile(path)
    assert meta['sofascore_bundle'] == path
    with open(path) as fh:
        assert json.load(fh)['event_id'] == 14025519


def test_explicit_event_id_skips_url_resolution(job, monkeypatch):
    client = FakeClient()
    _install(monkeypatch, client)

    pipeline.enrich_xg(job, 999, sofascore_url='https://www.sofascore.com/x/y')

    assert client.resolved_urls == []
    assert _meta(job)['sofascore_event_id'] == 999


# --- the sides check --------------------------------------------------------

def test_reversed_sides_are_swapped_not_written_blind(job, monkeypatch):
    """SofaScore lists the same match the other way round."""
    _install(monkeypatch, FakeClient(bundle=_bundle(home='Chelsea', away='Barcelona')))

    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')

    # Barcelona is home in the WhoScored job, so it must get the *away* figures
    # from a bundle that has the teams reversed.
    assert meta['home_xg'] == 0.2
    assert meta['away_xg'] == 0.6
    assert meta['xg_source'] == 'sofascore (sides swapped)'


def test_a_different_match_writes_nothing(job, monkeypatch):
    """The dominant failure mode of a two-link entry point: the wrong link."""
    _install(monkeypatch, FakeClient(bundle=_bundle(home='Inter', away='Napoli')))

    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')

    assert meta['home_xg'] == 0.0
    assert meta['away_xg'] == 0.0
    assert 'unavailable' in meta['xg_source']
    assert 'Inter' in meta['xg_source']
    assert 'sofascore_event_id' not in meta


# --- degrading without blowing up ------------------------------------------

def test_no_xg_in_the_feed_falls_back_to_fotmob_when_available(job, monkeypatch):
    _install(monkeypatch, FakeClient(bundle=_bundle(shots=[])))
    from scraper import fotmob

    class FM:
        def find_match(self, *a, **k):
            return 55

        def fetch_match(self, match_id):
            return {
                'general': {'homeTeam': {'id': 1, 'name': 'Barcelona'},
                            'awayTeam': {'id': 2, 'name': 'Chelsea'}},
                'header': {'teams': [{'name': 'Barcelona'}, {'name': 'Chelsea'}]},
                'content': {
                    'stats': {'Periods': {'All': {'stats': [{
                        'stats': [{'title': 'Expected goals (xG)',
                                   'stats': ['1.1', '0.3']},
                                  {'title': 'xG on target (xGOT)',
                                   'stats': ['0.9', '0.1']}],
                    }]}}},
                    'shotmap': {'shots': []},
                },
            }

    monkeypatch.setattr(fotmob, 'FotMobClient', lambda *a, **k: FM())
    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')
    assert meta['xg_source'] == 'fotmob'
    assert meta['home_xg'] == 1.1


def test_no_xg_in_the_feed_is_recorded_when_fotmob_also_misses(job, monkeypatch):
    _install(monkeypatch, FakeClient(bundle=_bundle(shots=[])))
    _block_fotmob(monkeypatch, raises=RuntimeError('fotmob down'))

    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')

    assert meta['home_xg'] == 0.0
    assert 'fotmob' in meta['xg_source']


def test_client_failure_leaves_metadata_intact(job, monkeypatch):
    _install(monkeypatch, FakeClient(raises=RuntimeError('sofascore down')))
    _block_fotmob(monkeypatch, raises=RuntimeError('fotmob down'))

    meta = pipeline.enrich_xg(job, sofascore_url='https://www.sofascore.com/x/y')

    assert meta['home_xg'] == 0.0
    assert 'sofascore down' in meta['xg_source']
    assert _meta(job)['teams'] == {'2817': 'Barcelona', '15': 'Chelsea'}


def test_search_fallback_is_off_for_other_clubs(job, monkeypatch):
    """find_event walks Arsenal's calendar; it is meaningless for Barcelona."""
    client = FakeClient()
    _install(monkeypatch, client)
    _block_fotmob(monkeypatch)

    meta = pipeline.enrich_xg(job, search_fallback=False)

    assert client.searched is False
    assert 'no sofascore event matched' in meta['xg_source']


def test_search_fallback_still_runs_for_arsenal(job, monkeypatch):
    client = FakeClient()
    _install(monkeypatch, client)

    pipeline.enrich_xg(job, search_fallback=True)

    assert client.searched is True


# --- the swap decision in isolation -----------------------------------------

@pytest.mark.parametrize('sofa,expected', [
    (('Barcelona', 'Chelsea'), False),
    (('Chelsea', 'Barcelona'), True),
    (('Inter', 'Napoli'), None),
    (('Barcelona', 'Napoli'), None),          # half a match is not a match
])
def test_sides_swapped(sofa, expected):
    assert pipeline._sides_swapped(
        sofa, ('Barcelona', 'Chelsea'), sofascore.names_match) is expected
