"""The two-link entry point: stage order, club routing, and the CLI's guards.

Every stage is stubbed - this is about what run_match calls, with what, and in
what order, not about whether the panels render.
"""

import pytest

from orchestrator import pipeline, run_match as cli


@pytest.fixture
def stub(monkeypatch):
    """Replace every stage with a recorder."""
    calls = []

    class FakeScraper:
        def __init__(self, *a, **k):
            pass

        def scrape_match(self, url, path, wait_time=15):
            calls.append(('scrape', {'url': url, 'wait_time': wait_time}))
            open(path, 'w').close()

        def close(self):
            pass

    import scraper.scraper as scraper_mod
    monkeypatch.setattr(scraper_mod, 'WhoscoredScraper', FakeScraper)

    def record(name, result=None, **fixed):
        def fn(*args, **kwargs):
            calls.append((name, kwargs))
            return result
        return fn

    monkeypatch.setattr(pipeline, 'extract_match_json', record('extract'))
    monkeypatch.setattr(pipeline, 'run_transform', record('transform'))
    monkeypatch.setattr(pipeline, 'enrich_xg', record('enrich', {
        'xg_source': 'sofascore', 'sofascore_event_id': 42,
        'home_xg': 1.8, 'away_xg': 0.6}))
    monkeypatch.setattr(pipeline, 'run_quality', record('quality', {
        'passed': True, 'summary': '20/20 passed'}))
    monkeypatch.setattr(pipeline, 'run_render', record('render', {
        'match_dir': '/out/barcelona/2025-2026/mw06_x', 'index': '/out/_index.json'}))
    monkeypatch.setattr(pipeline, 'run_load', record('load', 123456))
    return calls


def _names(calls):
    return [name for name, _ in calls]


def _kwargs(calls, name):
    return next(kw for n, kw in calls if n == name)


# --- stage sequence ---------------------------------------------------------

def test_enrichment_runs_between_transform_and_the_quality_gate(stub, tmp_root):
    """The step the old success path skipped entirely."""
    pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                       sofascore_url='https://sofascore/y', club='barcelona')

    assert _names(stub) == ['scrape', 'extract', 'transform', 'enrich',
                            'quality', 'render', 'load']


def test_the_sofascore_url_reaches_enrichment(stub, tmp_root):
    pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                       sofascore_url='https://sofascore/y', club='barcelona')
    assert _kwargs(stub, 'enrich')['sofascore_url'] == 'https://sofascore/y'


def test_result_reports_the_xg_it_found(stub, tmp_root):
    result = pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                                sofascore_url='https://sofascore/y', club='barcelona')
    assert result['xg_source'] == 'sofascore'
    assert result['xg'] == [1.8, 0.6]
    assert result['sofascore_event_id'] == 42
    assert result['match_id'] == 123456
    assert result['schema'] == 'barcelona'


def test_quality_failure_stops_before_rendering(stub, tmp_root, monkeypatch):
    monkeypatch.setattr(pipeline, 'run_quality',
                        lambda *a, **k: {'passed': False, 'summary': '2 error(s)'})
    with pytest.raises(ValueError, match='quality gate failed'):
        pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                           sofascore_url='https://sofascore/y', club='barcelona')
    assert 'render' not in _names(stub)


def test_no_load_skips_the_warehouse(stub, tmp_root):
    result = pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                                sofascore_url='https://sofascore/y', club='barcelona',
                                load_to_warehouse=False)
    assert 'load' not in _names(stub)
    assert result['match_id'] is None


# --- club routing -----------------------------------------------------------

def test_club_reaches_the_loader_and_the_renderer(stub, tmp_root):
    pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                       sofascore_url='https://sofascore/y', club='barcelona')
    assert _kwargs(stub, 'load')['club'] == 'barcelona'
    assert _kwargs(stub, 'render')['club'] == 'barcelona'


def test_arsenal_keeps_the_flat_output_layout(stub, tmp_root):
    """gunners predates the club level; nesting it would split its index in two."""
    pipeline.run_match('job-1', 'https://whoscored/x', '2026/2027',
                       sofascore_url='https://sofascore/y', club='gunners')
    assert _kwargs(stub, 'render')['club'] is None
    assert _kwargs(stub, 'load')['club'] == 'gunners'          # but the schema stands


def test_no_club_at_all_is_arsenal(stub, tmp_root):
    result = pipeline.run_match('job-1', 'https://whoscored/x', '2026/2027')
    assert _kwargs(stub, 'render')['club'] is None
    assert _kwargs(stub, 'load')['club'] is None
    assert result['schema'] == 'gunners'


def test_calendar_search_is_arsenal_only(stub, tmp_root):
    """find_event walks Arsenal's SofaScore calendar - useless for other clubs."""
    pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                       sofascore_url='https://sofascore/y', club='barcelona')
    assert _kwargs(stub, 'enrich')['search_fallback'] is False

    stub.clear()
    pipeline.run_match('job-2', 'https://whoscored/x', '2026/2027')
    assert _kwargs(stub, 'enrich')['search_fallback'] is True


def test_competition_reaches_the_transform(stub, tmp_root):
    pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                       sofascore_url='https://sofascore/y', club='barcelona',
                       competition='Champions League', matchweek=6)
    kw = _kwargs(stub, 'transform')
    assert kw['competition'] == 'Champions League'
    assert kw['matchweek'] == 6


def test_on_stage_is_called_for_each_stage(stub, tmp_root):
    seen = []
    pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                       sofascore_url='https://sofascore/y', club='barcelona',
                       on_stage=lambda stage, msg: seen.append(stage))
    assert seen == ['scrape', 'transform', 'enrich', 'validate', 'visualize', 'load']


# --- the SofaScore fallback -------------------------------------------------

def test_falls_back_when_whoscored_has_no_event_stream(stub, tmp_root, monkeypatch):
    def no_events(path):
        raise pipeline.NoEventDataError('only the summary feed is present')

    monkeypatch.setattr(pipeline, 'extract_match_json', no_events)
    monkeypatch.setattr(pipeline, 'run_sofascore',
                        lambda job, url, **kw: {'source': 'sofascore',
                                                'match_dir': '/out/x', 'panels': 10})

    result = pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026',
                                sofascore_url='https://sofascore/y', club='barcelona')
    assert result['source'] == 'sofascore'
    assert result['schema'] == 'barcelona'
    assert 'summary feed' in result['whoscored_error']


def test_no_fallback_source_reraises(stub, tmp_root, monkeypatch):
    def no_events(path):
        raise pipeline.NoEventDataError('only the summary feed is present')

    monkeypatch.setattr(pipeline, 'extract_match_json', no_events)
    with pytest.raises(pipeline.NoEventDataError, match='No sofascore_url supplied'):
        pipeline.run_match('job-1', 'https://whoscored/x', '2025/2026')


# --- the CLI's guards -------------------------------------------------------

def test_cli_requires_a_sofascore_url(capsys):
    code = cli.main(['--club', 'barcelona', '--whoscored', 'https://whoscored/x'])
    assert code == 2
    assert 'no xG' in capsys.readouterr().err


def test_cli_rejects_a_bad_club_before_scraping(capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli.pipeline, 'run_match', lambda *a, **k: called.append(1))

    code = cli.main(['--club', 'public', '--whoscored', 'https://whoscored/x',
                     '--sofascore', 'https://sofascore/y'])
    assert code == 2
    assert called == []
    assert 'reserved' in capsys.readouterr().err


def test_cli_normalises_the_club_into_a_schema(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, 'ensure_schema', lambda s: seen.setdefault('schema', s))
    monkeypatch.setattr(cli.pipeline, 'run_match',
                        lambda *a, **k: seen.update(k) or {'source': 'whoscored',
                                                           'match_dir': '/out'})

    code = cli.main(['--club', 'AC Milan', '--whoscored', 'https://whoscored/x',
                     '--sofascore', 'https://sofascore/y'])
    assert code == 0
    assert seen['schema'] == 'ac_milan'
    assert seen['club'] == 'ac_milan'


def test_cli_no_xg_runs_without_a_sofascore_url(monkeypatch):
    monkeypatch.setattr(cli, 'ensure_schema', lambda s: s)
    monkeypatch.setattr(cli.pipeline, 'run_match',
                        lambda *a, **k: {'source': 'whoscored', 'match_dir': '/out'})
    assert cli.main(['--club', 'barcelona', '--whoscored', 'https://whoscored/x',
                     '--no-xg']) == 0


def test_cli_returns_one_on_pipeline_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'ensure_schema', lambda s: s)

    def boom(*a, **k):
        raise RuntimeError('scrape blocked')

    monkeypatch.setattr(cli.pipeline, 'run_match', boom)
    code = cli.main(['--club', 'barcelona', '--whoscored', 'https://whoscored/x',
                     '--sofascore', 'https://sofascore/y'])
    assert code == 1
    assert 'scrape blocked' in capsys.readouterr().err


def test_cli_mode_flag_sets_the_browser_mode(monkeypatch):
    monkeypatch.delenv('GUNNERS_BROWSER_MODE', raising=False)
    monkeypatch.setattr(cli, 'ensure_schema', lambda s: s)
    monkeypatch.setattr(cli.pipeline, 'run_match',
                        lambda *a, **k: {'source': 'whoscored', 'match_dir': '/out'})

    cli.main(['--club', 'barcelona', '--whoscored', 'https://whoscored/x',
              '--sofascore', 'https://sofascore/y', '--mode', 'headless'])
    import os
    assert os.environ['GUNNERS_BROWSER_MODE'] == 'headless'
