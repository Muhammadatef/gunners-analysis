"""Importable pipeline stages, shared by the FastAPI orchestrator and Airflow.

The FastAPI service historically shelled out to each service's CLI. Airflow
needs the same steps as plain function calls (so failures raise, and retries are
cheap), so the logic lives here and both callers use it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys

ROOT = os.environ.get('GUNNERS_ROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger(__name__)

RAW_DIR = os.path.join(ROOT, 'data', 'raw')
PROCESSED_DIR = os.path.join(ROOT, 'data', 'processed')

# WhoScored's payload never names the competition. This repo started as an
# Arsenal/PL project, so that stays the default - pass competition= for anything
# else (Champions League, La Liga, Egyptian Premier League, ...).
DEFAULT_COMPETITION = os.getenv('GUNNERS_DEFAULT_COMPETITION', 'Premier League')

# Placeholders for clubs outside the curated Premier League colour map.
FALLBACK_HOME_COLOR = '#1B458F'
FALLBACK_AWAY_COLOR = '#EF0107'

JSON_RE = re.compile(r'(?<=require\.config\.params\["args"\].=.)[\s\S]*?;')
TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)


class NoEventDataError(ValueError):
    """WhoScored served the page but it carries no Opta event stream."""


def _diagnose(html: str, html_path: str) -> str:
    """Explain *why* there is no matchCentreData - the three causes look alike.

    1. bot wall / challenge page       -> retrying later may work
    2. page fine, no event data        -> retrying will never work
    3. genuinely truncated download    -> retry
    """
    title = TITLE_RE.search(html)
    title = title.group(1).strip()[:120] if title else '(no title)'

    if any(m in html for m in ('_Incapsula_Resource', 'Request unsuccessful',
                               'Access Denied', 'captcha')):
        return (f'{html_path}: blocked by WhoScored bot protection '
                f'(title: {title!r}). Retrying later usually works.')

    if len(html) < 50_000:
        return (f'{html_path}: page is only {len(html)} bytes (title: {title!r}) - '
                'the download looks truncated, retry.')

    if 'initialMatchDataForScrappers' in html:
        return (f'{html_path}: WhoScored published this match ({title!r}) but not its '
                'event data - only the summary feed is present, and there is no Match '
                'Centre tab. Common for friendlies and lower-tier competitions. The 18 '
                'panels are all derived from the event stream, so this match cannot be '
                'rendered. Retrying will not help.')

    return (f'{html_path}: matchCentreData missing and the page shape is unfamiliar '
            f'(title: {title!r}, {len(html)} bytes).')


def extract_match_json(html_path: str) -> str:
    """Pull the matchCentreData blob out of a scraped page and save it as JSON.

    Writes `<html_path minus .html>.json` and returns that path. Keeping the
    parsed JSON on disk means every downstream step (transform, visualise,
    load) reads the same bytes and none of them re-parse a 2 MB HTML file.
    """
    with open(html_path, encoding='utf-8') as fh:
        html = fh.read()

    found = JSON_RE.findall(html)
    if not found or 'matchCentreData' not in html:
        raise NoEventDataError(_diagnose(html, html_path))

    txt = found[0]
    for key in ('matchId', 'matchCentreData', 'matchCentreEventTypeJson', 'formationIdNameMappings'):
        txt = txt.replace(key, f'"{key}"')
    txt = txt.replace('};', '}')

    data = json.loads(txt)
    out = html_path[:-5] + '.json' if html_path.endswith('.html') else html_path + '.json'
    with open(out, 'w') as fh:
        json.dump(data, fh)
    logger.info('extracted match json -> %s (%d events)', out,
                len(data['matchCentreData']['events']))
    return out


def run_transform(job_id: str, fixture: dict | None = None, season: str | None = None,
                  xg: dict | None = None, *, competition: str | None = None,
                  matchweek: int | None = None, colors: dict | None = None) -> str:
    """Transform the scraped HTML into EventData/PlayerData/metadata.

    `competition` and `colors` matter off the Arsenal path: WhoScored's feed
    carries neither, so without them a Champions League match is filed as
    'Premier League' and a Barcelona report renders in Arsenal's palette.
    """
    from transformer.transform import FootballDataTransformer

    html_path = os.path.join(RAW_DIR, f'match_{job_id}.html')
    out_dir = os.path.join(PROCESSED_DIR, job_id)

    transformer = FootballDataTransformer()
    transformer.transform(html_path, out_dir)

    meta_path = os.path.join(out_dir, 'metadata.json')
    with open(meta_path) as fh:
        meta = json.load(fh)

    raw_json = os.path.join(RAW_DIR, f'match_{job_id}.json')
    if os.path.exists(raw_json):
        with open(raw_json) as fh:
            mcd = json.load(fh)['matchCentreData']
        meta['date'] = (mcd.get('startDate') or mcd.get('startTime') or '')[:10]
        meta['venue'] = mcd.get('venueName')

    if fixture:
        meta['gameweek'] = fixture.get('matchweek', meta.get('gameweek'))
        meta.setdefault('date', fixture['date'])
    if matchweek is not None:
        meta['gameweek'] = matchweek
    if season:
        meta['season'] = season
    meta['league'] = competition or (
        meta.get('league') if meta.get('league') not in (None, 'TBD') else DEFAULT_COMPETITION)

    # xG/xGOT are not in the WhoScored feed. They stay null until someone
    # supplies them (Fotmob) - the panels render "0.0" rather than a fake value.
    for k, v in (xg or {}).items():
        meta[k] = v
    meta.setdefault('xg_source', 'manual' if xg else None)

    meta.setdefault('bg_color', '#FFFFFF')
    meta.setdefault('line_color', '#000000')
    for key, value in (colors or {}).items():
        if value:
            meta[key] = value
    _apply_team_colors(meta, colors)

    with open(meta_path, 'w') as fh:
        json.dump(meta, fh, indent=2)
    return out_dir


def _apply_team_colors(meta: dict, colors: dict | None = None) -> None:
    """Pick home/away colours that can actually be told apart.

    The curated map covers the Premier League only, so for any other club the
    fallbacks are placeholders - pass --home-color / --away-color to run_match
    rather than shipping a Barcelona report in Arsenal red.

    Whatever the colours come from, the pair is checked before it is used: six
    clubs wear a red within a few OKLab units of Arsenal's, and a match drawn
    in one indivisible red is not a report. See visualizer/palette.py.

    A caller's colours arrive in `colors`, never by being read back out of
    `meta`. The distinction matters on a re-render: metadata.json already holds
    whatever was chosen last time, and treating that as a deliberate choice
    would pin a match to colours this function has since learned to reject.
    """
    from visualizer.palette import resolve

    teams = meta.get('teams') or {}
    ids = list(teams.keys())
    if len(ids) != 2:
        return

    supplied = colors or {}
    home, away = resolve(teams[ids[0]], teams[ids[1]],
                         home=supplied.get('home_color'),
                         away=supplied.get('away_color'))
    meta['home_color'], meta['away_color'] = home, away


def _sides_swapped(sofascore: tuple, whoscored: tuple, matches) -> bool | None:
    """Do the two feeds agree on which club is at home?

    Returns False when they line up, True when they are the wrong way round, and
    None when they describe different matches entirely.

    Worth the ceremony because home/away identity is positional everywhere in
    this pipeline - it comes from dict insertion order, not from a flag - and
    nothing else cross-checks the two sources. Pasting the wrong SofaScore link
    would otherwise write confident, wrong xG into matches.home_xg.
    """
    ss_home, ss_away = sofascore
    ws_home, ws_away = whoscored
    if matches(ws_home, ss_home) and matches(ws_away, ss_away):
        return False
    if matches(ws_home, ss_away) and matches(ws_away, ss_home):
        return True
    return None


def _meta_home_away(meta: dict) -> tuple[str, str] | None:
    """Resolve WhoScored home/away names from metadata (not dict value order)."""
    teams_map = meta.get('teams') or {}
    home_id = meta.get('home_team_id')
    away_id = meta.get('away_team_id')
    if home_id is not None and away_id is not None:
        home = teams_map.get(str(home_id)) or teams_map.get(home_id)
        away = teams_map.get(str(away_id)) or teams_map.get(away_id)
        if home and away:
            return str(home), str(away)
    teams = list(teams_map.values())
    if len(teams) == 2:
        return str(teams[0]), str(teams[1])
    return None


def _apply_xg(meta: dict, xg: dict, source: str, *, event_key: str | None = None,
              event_id: int | None = None) -> None:
    for key, value in xg.items():
        if value is not None:
            meta[key] = value
    meta['xg_source'] = source
    if event_key and event_id is not None:
        meta[event_key] = event_id


def _enrich_xg_sofascore(meta: dict, date: str, home: str, away: str, job_id: str, *,
                         sofascore_event_id: int | None, sofascore_url: str | None,
                         save_bundle: bool, with_heatmaps: bool,
                         search_fallback: bool) -> bool:
    """Try SofaScore. Returns True if xG was written into meta."""
    from scraper.sofascore import (SofaScoreClient, bundle_teams, names_match,
                                   save_bundle as write_bundle, xg_from_bundle)

    client = SofaScoreClient()
    event_id = sofascore_event_id
    if not event_id and sofascore_url:
        event_id = client.resolve_event_id(sofascore_url)
    if not event_id and search_fallback:
        event_id = client.find_event(date, home, away)

    if not event_id:
        meta['xg_source'] = 'unavailable: no sofascore event matched'
        return False

    bundle = client.fetch_match(event_id, with_heatmaps=with_heatmaps)
    if save_bundle:
        os.makedirs(RAW_DIR, exist_ok=True)
        bundle_path = os.path.join(RAW_DIR, f'sofascore_{job_id}.json')
        write_bundle(bundle, bundle_path)
        meta['sofascore_bundle'] = bundle_path

    xg = xg_from_bundle(bundle)
    swapped = _sides_swapped(bundle_teams(bundle), (home, away), names_match)

    if swapped is None:
        ss_home, ss_away = bundle_teams(bundle)
        meta['xg_source'] = (f'unavailable: sofascore teams do not match '
                             f'({ss_home} v {ss_away})')
        logger.warning('sofascore event %s is %s v %s, but this job is %s v %s '
                       '- refusing to write its xG',
                       event_id, ss_home, ss_away, home, away)
        return False
    if xg.get('home_xg') is None:
        meta['xg_source'] = f'unavailable: sofascore event {event_id} has no xG'
        return False

    if swapped:
        xg = {'home_xg': xg['away_xg'], 'away_xg': xg['home_xg'],
              'home_xgot': xg['away_xgot'], 'away_xgot': xg['home_xgot']}
    source = 'sofascore (sides swapped)' if swapped else 'sofascore'
    _apply_xg(meta, xg, source, event_key='sofascore_event_id', event_id=event_id)
    logger.info('xg from sofascore %s: %s-%s xG, %s-%s xGOT%s', event_id,
                xg['home_xg'], xg['away_xg'], xg['home_xgot'], xg['away_xgot'],
                ' (sides swapped)' if swapped else '')
    return True


def _enrich_xg_fotmob(meta: dict, date: str, home: str, away: str, job_id: str, *,
                      save_bundle: bool = True) -> bool:
    """FotMob fallback when SofaScore is 403 / missing. Returns True on success."""
    from scraper.fotmob import FotMobClient, teams_from_details, xg_from_details
    from scraper.sofascore import names_match

    client = FotMobClient()
    match_id = client.find_match(date, home, away)
    if not match_id:
        prev = meta.get('xg_source') or ''
        if prev.startswith('unavailable:'):
            meta['xg_source'] = f'{prev}; no fotmob match matched'
        else:
            meta['xg_source'] = 'unavailable: no fotmob match matched'
        return False

    details = client.fetch_match(match_id)
    if save_bundle:
        os.makedirs(RAW_DIR, exist_ok=True)
        bundle_path = os.path.join(RAW_DIR, f'fotmob_{job_id}.json')
        with open(bundle_path, 'w') as fh:
            json.dump(details, fh)
        meta['fotmob_bundle'] = bundle_path

    xg = xg_from_details(details)
    swapped = _sides_swapped(teams_from_details(details), (home, away), names_match)

    if swapped is None:
        fm_home, fm_away = teams_from_details(details)
        meta['xg_source'] = (f'unavailable: fotmob teams do not match '
                             f'({fm_home} v {fm_away})')
        return False
    if xg.get('home_xg') is None:
        meta['xg_source'] = f'unavailable: fotmob match {match_id} has no xG'
        return False

    if swapped:
        xg = {'home_xg': xg['away_xg'], 'away_xg': xg['home_xg'],
              'home_xgot': xg['away_xgot'], 'away_xgot': xg['home_xgot']}
    source = 'fotmob (sides swapped)' if swapped else 'fotmob'
    _apply_xg(meta, xg, source, event_key='fotmob_match_id', event_id=match_id)
    logger.info('xg from fotmob %s: %s-%s xG, %s-%s xGOT%s', match_id,
                xg['home_xg'], xg['away_xg'], xg['home_xgot'], xg['away_xgot'],
                ' (sides swapped)' if swapped else '')
    return True


def enrich_xg(job_id: str, sofascore_event_id: int | None = None, *,
              sofascore_url: str | None = None, save_bundle: bool = True,
              with_heatmaps: bool = False, search_fallback: bool = True) -> dict:
    """Fill the xG gap in a WhoScored match using SofaScore, then FotMob.

    WhoScored's feed has no xG anywhere. SofaScore is preferred (shot map +
    xGOT), but its API often returns 403 from Docker/datacenter IPs. FotMob's
    public match API is the fallback for the same team-level xG/xGOT fields.

    Never fatal: if neither source works, metadata keeps existing values and
    `xg_source` records what happened.
    """
    meta_path = os.path.join(PROCESSED_DIR, job_id, 'metadata.json')
    with open(meta_path) as fh:
        meta = json.load(fh)

    sides = _meta_home_away(meta)
    if not sides:
        logger.warning('cannot enrich xg for %s: teams missing from metadata', job_id)
        return meta
    home, away = sides

    # Trust the scraped feed over metadata.json - the latter can carry a stale
    # placeholder date from whatever created the job.
    date = meta.get('date', '')
    raw_json = os.path.join(RAW_DIR, f'match_{job_id}.json')
    if os.path.exists(raw_json):
        with open(raw_json) as fh:
            mcd = json.load(fh).get('matchCentreData', {})
        actual = (mcd.get('startDate') or mcd.get('startTime') or '')[:10]
        if actual and actual != date:
            logger.info('using feed date %s over metadata date %s', actual, date)
            date = actual
            meta['date'] = actual

    sofa_err = None
    try:
        if _enrich_xg_sofascore(
                meta, date, home, away, job_id,
                sofascore_event_id=sofascore_event_id,
                sofascore_url=sofascore_url,
                save_bundle=save_bundle,
                with_heatmaps=with_heatmaps,
                search_fallback=search_fallback):
            with open(meta_path, 'w') as fh:
                json.dump(meta, fh, indent=2)
            return meta
    except Exception as exc:  # noqa: BLE001
        sofa_err = exc
        logger.warning('sofascore xg failed for %s: %s', job_id, exc)

    # FotMob covers bot-walls and missing SofaScore xG — not a deliberate
    # "wrong match URL" refuse from the sides check.
    src = meta.get('xg_source') or ''
    try_fotmob = bool(sofa_err) or any(token in src for token in (
        'no sofascore event matched',
        'has no xG',
    ))
    if not try_fotmob:
        with open(meta_path, 'w') as fh:
            json.dump(meta, fh, indent=2)
        return meta

    try:
        if _enrich_xg_fotmob(meta, date, home, away, job_id, save_bundle=save_bundle):
            with open(meta_path, 'w') as fh:
                json.dump(meta, fh, indent=2)
            return meta
    except Exception as exc:  # noqa: BLE001
        logger.warning('fotmob xg failed for %s: %s', job_id, exc)
        detail = f'sofascore: {sofa_err}; fotmob: {exc}' if sofa_err else str(exc)
        meta['xg_source'] = f'unavailable: {detail}'
        with open(meta_path, 'w') as fh:
            json.dump(meta, fh, indent=2)
        return meta

    if sofa_err and (meta.get('xg_source') or '').startswith('unavailable: no '):
        meta['xg_source'] = (
            f'unavailable: sofascore: {sofa_err}; {meta.get("xg_source")}'
        )
    elif sofa_err is None and (meta.get('xg_source') or '') == 'unavailable: no fotmob match matched':
        # Prefer the SofaScore reason that triggered the fallthrough.
        pass

    with open(meta_path, 'w') as fh:
        json.dump(meta, fh, indent=2)
    return meta



def run_quality(job_id: str, *, min_events: int | None = None,
                max_events: int | None = None,
                min_full_time_minute: int | None = None) -> dict:
    from quality.checks import Thresholds, validate_job

    overrides = {k: v for k, v in {
        'min_events': min_events, 'max_events': max_events,
        'min_full_time_minute': min_full_time_minute}.items() if v is not None}
    thresholds = Thresholds(**overrides) if overrides else None

    report = validate_job(job_id, ROOT, thresholds)
    logger.info('quality %s: %s', job_id, report['summary'])
    return report


def run_visualize(job_id: str, out_dir: str | None = None) -> str:
    from visualizer import match_report
    out_dir = out_dir or os.path.join(ROOT, 'data', 'visualizations', job_id)
    os.makedirs(out_dir, exist_ok=True)
    match_report.prepare(job_id)
    match_report.render_individual(out_dir)
    match_report.render_dashboard(out_dir)
    return out_dir


def run_render(job_id: str, season: str | None = None, matchweek: int | None = None,
               club: str | None = None, output_root: str | None = None,
               dpi: int = 150, theme: str = 'classic') -> dict:
    """Render into the season layout (output/[<club>/]<season>/<match>/).

    run_visualize above writes the older flat data/visualizations/<job>/ dir and
    is still what the API and the maintenance DAG use.
    """
    from visualizer import match_report
    return match_report.render_match(job_id, output_root=output_root, season=season,
                                     matchweek=matchweek, dpi=dpi, club=club,
                                     theme=theme)


def run_load(job_id: str, season: str, fixture_key: str | None = None,
             out_dir: str | None = None, club: str | None = None) -> int:
    from db.loader import load
    return load(job_id, season_name=season, fixture_key=fixture_key, out_dir=out_dir,
                club=club)


# ---------------------------------------------------------------------------
# SofaScore fallback
# ---------------------------------------------------------------------------

def run_sofascore(job_id: str, url_or_event_id: str, out_dir: str | None = None, *,
                  club: str | None = None, season: str | None = None,
                  matchweek: int | None = None, output_root: str | None = None) -> dict:
    """Render what SofaScore *can* support when WhoScored has no event stream.

    Returns a summary dict. This is a reduced report - nine panels instead of
    eighteen - because SofaScore publishes aggregates and a shot map but no
    pass-by-pass feed. Anything needing pass coordinates is simply absent.

    With `club` (or an explicit season) it writes into the season layout and
    gets a match.json manifest; otherwise it keeps the flat legacy directory.
    """
    from scraper.sofascore import SofaScoreClient, save_bundle
    from visualizer import sofascore_report

    client = SofaScoreClient()
    target = str(url_or_event_id)
    event_id = int(target) if target.isdigit() else client.resolve_event_id(target)

    os.makedirs(RAW_DIR, exist_ok=True)
    bundle_path = os.path.join(RAW_DIR, f'sofascore_{job_id}.json')
    save_bundle(client.fetch_match(event_id), bundle_path)

    result = {
        'source': 'sofascore',
        'event_id': event_id,
        'bundle': bundle_path,
        'panels': len(sofascore_report.PANELS) + 1,
        'note': 'reduced report - SofaScore has no pass-by-pass event feed',
    }

    if out_dir is None and (club or season):
        rendered = sofascore_report.render_match(
            bundle_path, output_root=output_root, season=season,
            matchweek=matchweek, club=club, job_id=job_id)
        result['out_dir'] = rendered['match_dir']
        result['match_dir'] = rendered['match_dir']
        result['index'] = rendered['index']
        return result

    out_dir = out_dir or os.path.join(ROOT, 'data', 'visualizations', job_id)
    os.makedirs(out_dir, exist_ok=True)
    sofascore_report.prepare(bundle_path)
    sofascore_report.render_individual(out_dir)
    sofascore_report.render_dashboard(out_dir)
    result['out_dir'] = out_dir
    return result


def run_match(job_id: str, whoscored_url: str, season: str,
              fixture_key: str | None = None, sofascore_url: str | None = None, *,
              club: str | None = None, competition: str | None = None,
              matchweek: int | None = None, xg: dict | None = None,
              colors: dict | None = None, load_to_warehouse: bool = True,
              output_root: str | None = None, min_events: int | None = None,
              dpi: int = 150, wait_time: int = 15, theme: str = 'classic',
              on_stage=None) -> dict:
    """Full pipeline for one match, from two links.

    WhoScored supplies the event stream (18 panels + the warehouse load);
    SofaScore supplies the xG and xGOT that WhoScored's feed simply does not
    carry. If WhoScored has no event stream at all, SofaScore becomes the source
    outright and the reduced nine-panel report is rendered instead of failing.

    `club` picks the warehouse schema and the output subdirectory. Leave it
    unset for Arsenal, which keeps the `gunners` schema and the flat layout.
    `on_stage(stage, message)` is called as each stage begins, so a caller can
    report progress without reimplementing the sequence.
    """
    from scraper.scraper import WhoscoredScraper

    def stage(name, message):
        logger.info('[%s] %s: %s', job_id, name, message)
        if on_stage:
            on_stage(name, message)

    # Arsenal's rendered output predates the club level, and the DAG still
    # writes output/<season>/. Keep one index per club by not nesting it.
    from db.connection import DEFAULT_SCHEMA
    output_club = None if (club is None or club == DEFAULT_SCHEMA) else club

    stage('scrape', 'fetching the match page')
    html = os.path.join(RAW_DIR, f'match_{job_id}.html')
    scraper = WhoscoredScraper()
    try:
        scraper.scrape_match(whoscored_url, html, wait_time=wait_time)
    finally:
        scraper.close()

    try:
        extract_match_json(html)
    except NoEventDataError as exc:
        logger.warning('falling back to SofaScore: %s', exc)
        if not sofascore_url:
            raise NoEventDataError(
                f'{exc}\n\nNo sofascore_url supplied, so there is no fallback source. '
                'Pass one to render the reduced nine-panel report.') from exc
        stage('sofascore', 'WhoScored has no event stream; rendering from SofaScore')
        result = run_sofascore(job_id, sofascore_url, club=output_club, season=season,
                               matchweek=matchweek, output_root=output_root)
        result['whoscored_error'] = str(exc)
        result['schema'] = club or DEFAULT_SCHEMA
        return result

    stage('transform', 'building event data')
    run_transform(job_id, season=season, xg=xg, competition=competition,
                  matchweek=matchweek, colors=colors)

    stage('enrich', 'pulling xG from SofaScore')
    # find_event walks Arsenal's SofaScore calendar, so it is only a sensible
    # fallback for Arsenal. Every other club must have supplied a URL.
    meta = enrich_xg(job_id, sofascore_url=sofascore_url,
                     search_fallback=(club is None or club == DEFAULT_SCHEMA))

    stage('validate', 'running quality checks')
    report = run_quality(job_id, min_events=min_events)
    if not report['passed']:
        raise ValueError(f'quality gate failed: {report["summary"]}')

    stage('visualize', 'rendering the panels')
    rendered = run_render(job_id, season=season, matchweek=matchweek,
                          club=output_club, output_root=output_root, dpi=dpi,
                          theme=theme)

    match_id = None
    if load_to_warehouse:
        stage('load', f'loading the {club or DEFAULT_SCHEMA} warehouse')
        match_id = run_load(job_id, season, fixture_key=fixture_key,
                            out_dir=os.path.join(rendered['match_dir'], 'panels'),
                            club=club)

    return {
        'source': 'whoscored',
        'job_id': job_id,
        'match_id': match_id,
        'match_dir': rendered['match_dir'],
        'out_dir': rendered['match_dir'],
        'index': rendered['index'],
        'schema': club or DEFAULT_SCHEMA,
        'xg_source': meta.get('xg_source'),
        'sofascore_event_id': meta.get('sofascore_event_id'),
        'xg': [meta.get('home_xg'), meta.get('away_xg')],
        'quality': report['summary'],
        'panels': 19,
    }
