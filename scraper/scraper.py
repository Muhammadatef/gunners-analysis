"""Whoscored match data scraper using Selenium.

Runs a real browser by default. Headless Chrome advertises itself in a dozen
small ways - a `HeadlessChrome` UA token, `navigator.webdriver`, no GPU, no
plugin surface, no input events ever - and WhoScored's front door notices. The
mode ladder below prefers an actual window, falls back to Xvfb (still real
Chrome, real renderer, just no visible display), and only goes headless if
neither is available.

    GUNNERS_BROWSER_MODE=headed|xvfb|headless    overrides the auto-detection

Requests are paced, not parallelised: this fetches one match page at a time,
which is the other half of not looking like a bot.
"""

import logging
import os
import random
import shutil
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BROWSER_MODES = ('headed', 'xvfb', 'headless')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROFILE_DIR = os.path.join(ROOT, 'data', 'browser-profile')


def _in_container() -> bool:
    """Sandbox flags are a container workaround; they weaken a desktop run."""
    return os.path.exists('/.dockerenv') or os.getenv('GUNNERS_ROOT') == '/opt/gunners'


def _default_profile_dir() -> str:
    """Host: persistent profile. Container: /tmp (repo volume often not writable by uid 50000)."""
    override = os.getenv('GUNNERS_BROWSER_PROFILE')
    if override:
        return override
    if _in_container():
        return '/tmp/gunners-browser-profile'
    return DEFAULT_PROFILE_DIR


# Leftover Chrome lock files after a crashed scrape block the next session.
_PROFILE_LOCK_NAMES = (
    'DevToolsActivePort',
    'SingletonLock',
    'SingletonCookie',
    'SingletonSocket',
    'lockfile',
)


def _clear_stale_profile_locks(profile_dir: str) -> None:
    """Drop lock files Chrome leaves behind when a previous session died.

    Safe when no live Chrome holds the profile: if one does, Chrome will still
    refuse the session and we fall back to a fresh temp profile below.
    """
    root = Path(profile_dir)
    if not root.is_dir():
        return
    for name in _PROFILE_LOCK_NAMES:
        path = root / name
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
                logger.info('removed stale Chrome profile lock %s', path)
        except OSError as exc:
            logger.warning('could not remove %s: %s', path, exc)


def resolve_mode(mode: str | None = None, headless: bool | None = None) -> str:
    """explicit mode > legacy headless kwarg > env > auto-detect."""
    if mode:
        if mode not in BROWSER_MODES:
            raise ValueError(f'unknown browser mode {mode!r}; expected one of {BROWSER_MODES}')
        return mode
    if headless is not None:
        return 'headless' if headless else 'headed'
    env = os.getenv('GUNNERS_BROWSER_MODE')
    if env:
        if env not in BROWSER_MODES:
            raise ValueError(f'GUNNERS_BROWSER_MODE={env!r}; expected one of {BROWSER_MODES}')
        return env
    if os.environ.get('DISPLAY'):
        return 'headed'
    if shutil.which('Xvfb'):
        return 'xvfb'
    return 'headless'


class WhoscoredScraper:
    """Scraper for Whoscored match pages."""

    def __init__(self, mode: str | None = None, headless: bool | None = None,
                 profile_dir: str | None = None, page_load_timeout: int = 45):
        """Initialize scraper.

        Args:
            mode: 'headed', 'xvfb' or 'headless'. Auto-detected when omitted.
            headless: legacy switch, kept so old call sites keep working.
            profile_dir: persistent Chrome profile. Cookies and any consent or
                challenge clearance survive between runs, which is what stops
                the second scrape of a session looking brand new.
            page_load_timeout: seconds before a hung renderer raises. Without
                this Selenium waits out its own 300s renderer timeout.
        """
        self.mode = resolve_mode(mode, headless)
        self.profile_dir = profile_dir or _default_profile_dir()
        self.page_load_timeout = page_load_timeout
        self.driver = None
        self._display = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def headless(self) -> bool:
        """Back-compat for anything still reading the old attribute."""
        return self.mode == 'headless'

    def _start_display(self):
        """Bring up a virtual display so real Chrome has somewhere to draw."""
        if self._display or os.environ.get('DISPLAY'):
            return
        try:
            from pyvirtualdisplay import Display
        except ImportError:
            logger.warning('xvfb mode requested but pyvirtualdisplay is not installed; '
                           'falling back to headless')
            self.mode = 'headless'
            return
        self._display = Display(visible=False, size=(1920, 1080))
        self._display.start()
        logger.info('virtual display started on %s', os.environ.get('DISPLAY'))

    def _chrome_options(self) -> Options:
        opts = Options()

        # Prefer the image's Chromium when set (Airflow/orchestrator Dockerfiles).
        chrome_bin = os.getenv('CHROME_BIN') or os.getenv('CHROMIUM_BIN')
        if chrome_bin and os.path.isfile(chrome_bin):
            opts.binary_location = chrome_bin

        if self.mode == 'headless':
            opts.add_argument('--headless=new')
            # Only headless needs the software-rendering workaround; leaving it
            # on for a real browser removes a GPU fingerprint a human would have.
            opts.add_argument('--disable-gpu')

        if _in_container():
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')

        # Jitter the window so the viewport is not byte-identical every run.
        width = 1920 + random.randint(-80, 0)
        height = 1080 + random.randint(-60, 0)
        opts.add_argument(f'--window-size={width},{height}')

        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_experimental_option('excludeSwitches', ['enable-automation'])
        opts.add_experimental_option('useAutomationExtension', False)

        # No pinned user-agent. The old one claimed Chrome/120 from a Chrome/151
        # binary, which is a mismatch any bot wall can check in one line. Chrome's
        # own UA always matches its own TLS and JS fingerprint.

        if self.profile_dir:
            os.makedirs(self.profile_dir, exist_ok=True)
            _clear_stale_profile_locks(self.profile_dir)
            opts.add_argument(f'--user-data-dir={self.profile_dir}')

        # 'eager' returns at DOMContentLoaded instead of waiting on every last
        # tracker; the match payload is inline in the document.
        opts.page_load_strategy = 'eager'
        return opts

    def _setup_driver(self):
        """Setup Chrome webdriver."""
        if self.mode == 'xvfb':
            self._start_display()

        logger.info('Setting up Chrome webdriver (mode=%s)', self.mode)
        chrome_options = self._chrome_options()

        try:
            self.driver = self._launch_chrome(chrome_options)
        except Exception as e:
            # Shared profile still locked (host scrape, zombie Chrome, root locks).
            # Retry once with an ephemeral profile so Airflow can finish the match.
            err = str(e).lower()
            locked = self.profile_dir and (
                'user-data-dir' in err
                or 'devtools' in err
                or 'session not created' in err
            )
            if locked:
                logger.warning(
                    'Chrome refused profile %s (%s); retrying with a fresh temp profile',
                    self.profile_dir, e)
                self.profile_dir = os.path.join(
                    '/tmp' if _in_container() else os.path.join(ROOT, 'data'),
                    f'gunners-browser-profile-run-{os.getpid()}-{int(time.time())}',
                )
                os.makedirs(self.profile_dir, exist_ok=True)
                chrome_options = self._chrome_options()
                self.driver = self._launch_chrome(chrome_options)
            else:
                raise

        self.driver.set_page_load_timeout(self.page_load_timeout)

        # navigator.webdriver is true under automation regardless of the flags
        # above; this runs before any page script can read it.
        try:
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            })
        except Exception as exc:                                     # noqa: BLE001
            logger.debug('could not install the webdriver shim: %s', exc)

        logger.info("Chrome webdriver setup complete")

    def _launch_chrome(self, chrome_options: Options):
        # Prefer the matching system driver when the image ships Chromium.
        driver_path = os.getenv('CHROMEDRIVER_PATH')
        if driver_path and os.path.isfile(driver_path):
            try:
                return webdriver.Chrome(
                    service=Service(executable_path=driver_path),
                    options=chrome_options,
                )
            except Exception as e:
                logger.warning('CHROMEDRIVER_PATH=%s failed: %s', driver_path, e)
        try:
            return webdriver.Chrome(options=chrome_options)
        except Exception as e:
            logger.warning('System chromedriver failed: %s, trying with webdriver-manager', e)
            try:
                service = Service(ChromeDriverManager().install())
                return webdriver.Chrome(service=service, options=chrome_options)
            except Exception as e2:
                logger.error('Both methods failed: %s', e2)
                service = Service(executable_path='/usr/bin/chromedriver')
                return webdriver.Chrome(service=service, options=chrome_options)

    # -- human-shaped pacing ------------------------------------------------

    @staticmethod
    def _pause(low: float = 0.4, high: float = 1.1):
        time.sleep(random.uniform(low, high))

    def _act_human(self):
        """A mouse move and a scroll. Cheap, and it beats 'no input events ever'."""
        try:
            body = self.driver.find_element(By.TAG_NAME, 'body')
            ActionChains(self.driver).move_to_element_with_offset(
                body, random.randint(30, 400), random.randint(30, 300)).perform()
            self._pause(0.2, 0.6)
            self.driver.execute_script(
                f'window.scrollBy(0, {random.randint(250, 700)});')
            self._pause(0.3, 0.9)
        except Exception as exc:                                     # noqa: BLE001
            logger.debug('human-pacing step skipped: %s', exc)

    def _wait_for_payload(self, deadline: float) -> str:
        """Poll for matchCentreData rather than sleeping a flat interval.

        A good page resolves in a couple of seconds; a blocked one fails at the
        deadline instead of burning the full wait every time.
        """
        html = ''
        while time.time() < deadline:
            html = self.driver.page_source
            if 'matchCentreData' in html:
                return html
            time.sleep(random.uniform(0.4, 0.9))
        return html or self.driver.page_source

    # -- scraping -----------------------------------------------------------

    def scrape_match(self, match_url: str, output_path: str, wait_time: int = 10,
                     attempts: int = 2) -> str:
        """Scrape match HTML from Whoscored.

        Args:
            match_url: Whoscored match URL
            output_path: Path to save HTML file
            wait_time: Seconds to keep polling for the match payload
            attempts: Retries with a fresh browser before giving up

        Returns:
            Path to saved HTML file
        """
        logger.info(f"Scraping match from {match_url}")
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                if not self.driver:
                    self._setup_driver()

                self.driver.get(match_url)
                self._pause()
                self._act_human()

                logger.info('waiting up to %ss for the match payload ...', wait_time)
                html_content = self._wait_for_payload(time.time() + wait_time)

                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.ID, "match-centre-header")))
                    logger.info("Match center header loaded")
                except Exception as e:
                    logger.warning(f"Could not find match center header: {e}")

                html_content = self.driver.page_source

                if 'matchCentreData' not in html_content:
                    logger.warning('matchCentreData not found in HTML '
                                   '(attempt %s/%s, %s bytes)',
                                   attempt, attempts, len(html_content))
                    if attempt < attempts:
                        raise RuntimeError('no match payload')

                output_file = Path(output_path)
                output_file.parent.mkdir(parents=True, exist_ok=True)
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(html_content)

                logger.info(f"HTML saved to {output_file} ({len(html_content)} bytes)")
                return str(output_file)

            except Exception as e:
                last_error = e
                logger.error('scrape attempt %s/%s failed: %s', attempt, attempts, e)
                self.close()
                if attempt < attempts:
                    backoff = random.uniform(5, 15)
                    logger.info('retrying in %.1fs with a fresh browser', backoff)
                    time.sleep(backoff)

        self.close()
        raise last_error if last_error else RuntimeError('scrape failed')

    def fetch_html(self, url: str, wait_time: int = 8) -> str:
        """Load a page and return its HTML, leaving the driver open for reuse.

        Used for listing pages (fixtures, tournament calendars) where we only
        need the links, not the match-centre payload.
        """
        if not self.driver:
            self._setup_driver()

        logger.info(f"Fetching {url}")
        self.driver.get(url)
        self._pause()
        self._act_human()
        time.sleep(min(wait_time, 3))
        return self.driver.page_source

    def close(self):
        """Shut the browser down if it is still open."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:                                        # noqa: BLE001
                pass
            finally:
                self.driver = None
        if self._display:
            try:
                self._display.stop()
            finally:
                self._display = None

    def healthcheck(self) -> bool:
        """Check if scraper is working.

        Returns:
            True if healthy, False otherwise
        """
        try:
            self._setup_driver()
            self.driver.get("https://www.whoscored.com")
            return "WhoScored" in self.driver.page_source
        except Exception as e:
            logger.error(f"Healthcheck failed: {e}")
            return False
        finally:
            self.close()


def main():
    """Main entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="Scrape Whoscored match data")
    parser.add_argument("match_url", help="Whoscored match URL")
    parser.add_argument("--output", default="/data/raw/match.html", help="Output HTML file path")
    parser.add_argument("--wait", type=int, default=15,
                        help="Seconds to poll for the match payload")
    parser.add_argument("--mode", choices=BROWSER_MODES, default=None,
                        help="Browser mode (default: headed if a display exists, else xvfb)")
    parser.add_argument("--attempts", type=int, default=2,
                        help="Retries with a fresh browser before giving up")
    parser.add_argument("--profile-dir", default=None,
                        help=f"Chrome profile directory (default: {DEFAULT_PROFILE_DIR})")

    args = parser.parse_args()

    scraper = WhoscoredScraper(mode=args.mode, profile_dir=args.profile_dir)
    try:
        path = scraper.scrape_match(args.match_url, args.output,
                                    wait_time=args.wait, attempts=args.attempts)
        print(f'saved {path}  (mode={scraper.mode})')
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
