"""Browser mode resolution and the Chrome flags it produces.

Headless Chrome is easy to fingerprint - a HeadlessChrome UA token, no GPU, no
input events - and WhoScored notices. The scraper therefore prefers a real
browser, and these tests pin the ladder that decides which it gets.
"""

import pytest

from scraper import scraper as scraper_mod
from scraper.scraper import WhoscoredScraper, resolve_mode


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv('GUNNERS_BROWSER_MODE', raising=False)
    monkeypatch.delenv('DISPLAY', raising=False)


# --- the ladder -------------------------------------------------------------

def test_explicit_mode_wins(monkeypatch):
    monkeypatch.setenv('GUNNERS_BROWSER_MODE', 'headless')
    monkeypatch.setenv('DISPLAY', ':0')
    assert resolve_mode('headed') == 'headed'


def test_legacy_headless_kwarg_still_works(monkeypatch):
    monkeypatch.setenv('DISPLAY', ':0')
    assert resolve_mode(None, headless=True) == 'headless'
    assert resolve_mode(None, headless=False) == 'headed'


def test_env_beats_autodetection(monkeypatch):
    monkeypatch.setenv('GUNNERS_BROWSER_MODE', 'xvfb')
    monkeypatch.setenv('DISPLAY', ':0')
    assert resolve_mode() == 'xvfb'


def test_auto_prefers_a_real_display(monkeypatch):
    monkeypatch.setenv('DISPLAY', ':0')
    assert resolve_mode() == 'headed'


def test_auto_falls_back_to_xvfb(monkeypatch):
    monkeypatch.setattr(scraper_mod.shutil, 'which',
                        lambda name: '/usr/bin/Xvfb' if name == 'Xvfb' else None)
    assert resolve_mode() == 'xvfb'


def test_auto_falls_back_to_headless_with_nothing_available(monkeypatch):
    monkeypatch.setattr(scraper_mod.shutil, 'which', lambda name: None)
    assert resolve_mode() == 'headless'


@pytest.mark.parametrize('bad', ['visible', 'HEADED', 'true', 'no-headless'])
def test_unknown_mode_is_rejected(bad):
    with pytest.raises(ValueError):
        resolve_mode(bad)


def test_unknown_env_mode_is_rejected(monkeypatch):
    monkeypatch.setenv('GUNNERS_BROWSER_MODE', 'invisible')
    with pytest.raises(ValueError):
        resolve_mode()


# --- the flags --------------------------------------------------------------

def _args(mode, monkeypatch, container=False):
    monkeypatch.setattr(scraper_mod, '_in_container', lambda: container)
    return WhoscoredScraper(mode=mode, profile_dir=None)._chrome_options().arguments


def test_headless_flag_only_in_headless_mode(monkeypatch):
    assert any('--headless' in a for a in _args('headless', monkeypatch))
    assert not any('--headless' in a for a in _args('headed', monkeypatch))
    assert not any('--headless' in a for a in _args('xvfb', monkeypatch))


def test_no_pinned_user_agent(monkeypatch):
    """The old flag claimed Chrome/120 from a much newer binary - a giveaway."""
    for mode in ('headed', 'xvfb', 'headless'):
        assert not any(a.startswith('user-agent') for a in _args(mode, monkeypatch))


def test_gpu_is_only_disabled_when_headless(monkeypatch):
    assert '--disable-gpu' in _args('headless', monkeypatch)
    assert '--disable-gpu' not in _args('headed', monkeypatch)


def test_sandbox_flags_are_container_only(monkeypatch):
    assert '--no-sandbox' not in _args('headed', monkeypatch, container=False)
    assert '--no-sandbox' in _args('headed', monkeypatch, container=True)


def test_automation_flag_is_always_disabled(monkeypatch):
    for mode in ('headed', 'xvfb', 'headless'):
        assert '--disable-blink-features=AutomationControlled' in _args(mode, monkeypatch)


def test_profile_dir_is_passed_when_set(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper_mod, '_in_container', lambda: False)
    profile = str(tmp_path / 'profile')
    args = WhoscoredScraper(mode='headed', profile_dir=profile)._chrome_options().arguments
    assert f'--user-data-dir={profile}' in args


def test_page_load_strategy_is_eager(monkeypatch):
    """So a hung renderer raises in ~45s instead of Selenium's own 300s."""
    monkeypatch.setattr(scraper_mod, '_in_container', lambda: False)
    opts = WhoscoredScraper(mode='headed', profile_dir=None)._chrome_options()
    assert opts.page_load_strategy == 'eager'


def test_headless_property_reflects_the_mode(monkeypatch):
    assert WhoscoredScraper(mode='headless').headless is True
    assert WhoscoredScraper(mode='headed').headless is False
