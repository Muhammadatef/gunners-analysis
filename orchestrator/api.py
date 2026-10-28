"""Ad-hoc HTTP entry point for the analytics pipeline.

Division of labour with Airflow
-------------------------------
Airflow owns the *season*: what runs, when, retries, backfills, and the state of
every fixture. Nothing here schedules anything.

This service owns *one-off* work that is not on the Arsenal calendar:

  * render a match that is not an Arsenal fixture (opponent scouting, another
    league) by posting its WhoScored URL, optionally with a SofaScore URL for xG
  * load another club into its own schema with `club`, leaving `gunners` to Arsenal
  * re-run a match with corrected xG/xGOT values without touching a DAG
  * read the season warehouse over HTTP for a dashboard or notebook

For a manual run you want to watch, `orchestrator/run_match.py` is the CLI
equivalent - same pipeline, synchronous, non-zero exit on failure.

Both callers execute the *same* code - `orchestrator/pipeline.py`. This module
is a thin HTTP wrapper over those functions; it does not reimplement them.
"""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import redis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

from db.connection import DEFAULT_SCHEMA, club_schema, ensure_schema, use_schema
from orchestrator import pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = pipeline.ROOT
SEASON = os.getenv('GUNNERS_SEASON', '2026/2027')

app = FastAPI(
    title='GunnersAnalysis - ad-hoc pipeline API',
    description='One-off match rendering and season warehouse queries. '
                'Scheduled Arsenal fixtures are run by Airflow, not here.',
    version='2.0.0',
)

redis_client = redis.Redis.from_url(
    os.getenv('REDIS_URL', 'redis://redis:6379'), decode_responses=True)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PipelineRequest(BaseModel):
    match_url: HttpUrl
    season: str = SEASON
    fixture_key: Optional[str] = None

    # The SofaScore URL for the same match. It is where xG and xGOT come from,
    # and it is also the fallback source when WhoScored has no event stream.
    sofascore_url: Optional[HttpUrl] = None

    # Which club's schema and output directory this match belongs to. None
    # means Arsenal: the `gunners` schema and the flat output layout.
    club: Optional[str] = None
    competition: Optional[str] = None
    matchweek: Optional[int] = None

    # Relax the Premier-League-calibrated event-count gate for a sparser feed.
    min_events: Optional[int] = None

    # Not present in the WhoScored feed - supply them, or a sofascore_url, or
    # the shot map and GK panels render 0.0 placeholders.
    home_xg: Optional[float] = None
    away_xg: Optional[float] = None
    home_xgot: Optional[float] = None
    away_xgot: Optional[float] = None

    load_to_warehouse: bool = True

    @field_validator('club')
    @classmethod
    def _valid_club(cls, v):
        # Reject here so a bad club is a 422 rather than a 500 raised inside a
        # background task, where the caller would only see it via /status.
        return club_schema(v) if v else v


class PipelineStatus(BaseModel):
    job_id: str
    status: str                      # pending | running | completed | failed
    stage: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


class VisualizationsList(BaseModel):
    job_id: str
    visualizations: List[str]
    # Where they actually are on disk - the season layout puts each match in its
    # own directory, so the file names alone are no longer enough to find them.
    match_dir: Optional[str] = None


# ---------------------------------------------------------------------------
# Job state (ephemeral; the durable audit trail is gunners.pipeline_runs)
# ---------------------------------------------------------------------------

def _set_status(job_id: str, status: str, stage: str = None,
                message: str = None, error: str = None) -> None:
    redis_client.setex(f'job:{job_id}', 86400, json.dumps({
        'job_id': job_id, 'status': status, 'stage': stage,
        'message': message, 'error': error}))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get('/')
async def root():
    return {
        'service': 'GunnersAnalysis',
        'role': 'ad-hoc runs and warehouse queries',
        'scheduled_runs': 'Airflow (see airflow/dags/)',
        'season': SEASON,
        'default_schema': DEFAULT_SCHEMA,
    }


@app.get('/health')
async def healthcheck():
    out: Dict[str, Any] = {'status': 'healthy'}
    try:
        redis_client.ping()
        out['redis'] = 'connected'
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(503, f'redis unavailable: {exc}')
    try:
        from db.connection import connect
        with connect() as conn:
            conn.cursor().execute('SELECT 1')
        out['postgres'] = 'connected'
    except Exception as exc:                                     # noqa: BLE001
        out['status'] = 'degraded'
        out['postgres'] = f'unavailable: {exc}'
    return out


@app.post('/pipeline/run', response_model=PipelineStatus)
async def run_pipeline(request: PipelineRequest, background_tasks: BackgroundTasks):
    """Run the full pipeline for one match URL, off-schedule."""
    job_id = str(uuid.uuid4())
    _set_status(job_id, 'pending', 'queued', 'job accepted')
    background_tasks.add_task(_execute, job_id, request)
    return PipelineStatus(job_id=job_id, status='pending', stage='queued',
                          message='Pipeline job queued')


def _execute(job_id: str, request: PipelineRequest) -> None:
    """Run the pipeline in-process, reporting each stage into redis.

    The stage sequence lives in pipeline.run_match - duplicating it here is how
    the SofaScore fallback used to be unreachable over HTTP.
    """
    try:
        xg = {k: v for k, v in {
            'home_xg': request.home_xg, 'away_xg': request.away_xg,
            'home_xgot': request.home_xgot, 'away_xgot': request.away_xgot,
        }.items() if v is not None}

        if request.club and request.load_to_warehouse:
            ensure_schema(request.club)

        with use_schema(request.club):
            result = pipeline.run_match(
                job_id, str(request.match_url), request.season,
                fixture_key=request.fixture_key,
                sofascore_url=str(request.sofascore_url) if request.sofascore_url else None,
                club=request.club,
                competition=request.competition,
                matchweek=request.matchweek,
                xg=xg or None,
                load_to_warehouse=request.load_to_warehouse,
                min_events=request.min_events,
                on_stage=lambda stage, message: _set_status(
                    job_id, 'running', stage, message),
            )

        out_dir = result.get('match_dir') or result.get('out_dir')
        _set_status(job_id, 'completed', 'done', f'outputs in {out_dir}')
        logger.info('[%s] pipeline completed', job_id)
    except Exception as exc:                                     # noqa: BLE001
        logger.exception('[%s] pipeline failed', job_id)
        _set_status(job_id, 'failed', 'error', error=str(exc))


@app.get('/pipeline/status/{job_id}', response_model=PipelineStatus)
async def get_pipeline_status(job_id: str):
    raw = redis_client.get(f'job:{job_id}')
    if not raw:
        raise HTTPException(404, f'job {job_id} not found')
    return PipelineStatus(**json.loads(raw))


@app.get('/visualizations/{job_id}', response_model=VisualizationsList)
async def list_visualizations(job_id: str):
    """Every PNG rendered for a job, wherever it landed.

    Two layouts coexist: the flat data/visualizations/<job>/ that run_visualize
    still writes, and the season layout that run_match uses
    (output/[<club>/]<season>/<match>/, dashboard at the top and panels one
    level down). Look in both, newest convention first.
    """
    from visualizer import output_layout

    match_dir = output_layout.find_match_dir(job_id)
    if match_dir:
        return VisualizationsList(
            job_id=job_id,
            match_dir=match_dir,
            visualizations=output_layout.match_panels(match_dir))

    viz_dir = Path(ROOT) / 'data' / 'visualizations' / job_id
    if viz_dir.is_dir():
        return VisualizationsList(
            job_id=job_id,
            match_dir=str(viz_dir),
            visualizations=sorted(p.name for p in viz_dir.glob('*.png')))

    raise HTTPException(404, f'no visualizations for {job_id}')


# --- season warehouse (read-only) ------------------------------------------

def _query(sql: str, params: tuple = (), club: Optional[str] = None) -> List[Dict[str, Any]]:
    from db.connection import connect
    with connect(schema=club_schema(club) if club else None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get('/season/progress')
async def season_progress(club: Optional[str] = None):
    """Every fixture, its status, and how many panels have been rendered.

    Only Arsenal has a seeded calendar, so this is empty for other clubs.
    """
    return _query('SELECT * FROM v_season_progress', club=club)


@app.get('/season/table')
async def season_table(club: Optional[str] = None):
    return _query('SELECT * FROM v_team_season_summary '
                  'ORDER BY points DESC, goal_difference DESC', club=club)


@app.get('/season/players')
async def season_players(team: Optional[str] = None, order_by: str = 'total_xt',
                         limit: int = 30, club: Optional[str] = None):
    allowed = {'total_xt', 'xt_per_90', 'goals', 'assists', 'minutes',
               'progressive_passes', 'prog_passes_per_90', 'key_passes', 'avg_rating'}
    if order_by not in allowed:
        raise HTTPException(400, f'order_by must be one of {sorted(allowed)}')
    if team:
        return _query(
            f'SELECT * FROM v_player_season_totals WHERE team_name = %s '
            f'ORDER BY {order_by} DESC NULLS LAST LIMIT %s', (team, limit), club=club)
    return _query(
        f'SELECT * FROM v_player_season_totals '
        f'ORDER BY {order_by} DESC NULLS LAST LIMIT %s', (limit,), club=club)


def _normalise_season_param(season: str) -> str:
    """Expand 2025/26 or 2025-2026 to warehouse form YYYY/YYYY."""
    import re
    text = season.strip().replace('-', '/')
    short = re.fullmatch(r'(\d{4})\s*/\s*(\d{2})', text)
    if short:
        start, end2 = short.group(1), short.group(2)
        return f'{start}/{start[:2]}{end2}'
    long = re.fullmatch(r'(\d{4})\s*/\s*(\d{4})', text)
    if long:
        return f'{long.group(1)}/{long.group(2)}'
    return text


@app.get('/matches')
async def list_matches(club: Optional[str] = None,
                       season: Optional[str] = None,
                       competition: Optional[str] = None,
                       limit: int = Query(100, ge=1, le=500)):
    """Match grid: newest first, optional season / competition filter."""
    clauses = ['1=1']
    params: list[Any] = []
    if season:
        clauses.append('s.name = %s')
        params.append(_normalise_season_param(season))
    if competition:
        clauses.append('m.competition ILIKE %s')
        params.append(f'%{competition}%')
    params.append(limit)
    where = ' AND '.join(clauses)
    return _query(f'''
        SELECT m.match_id, m.match_date, m.matchweek, m.competition,
               m.home_score, m.away_score, m.home_xg, m.away_xg,
               m.home_xgot, m.away_xgot, m.venue_name,
               h.name AS home_team, a.name AS away_team,
               s.name AS season
        FROM matches m
        JOIN seasons s ON s.season_id = m.season_id
        JOIN teams h ON h.team_id = m.home_team_id
        JOIN teams a ON a.team_id = m.away_team_id
        WHERE {where}
        ORDER BY m.match_date DESC NULLS LAST, m.match_id DESC
        LIMIT %s
    ''', tuple(params), club=club)


@app.get('/matches/{match_id}')
async def match_detail(match_id: int, club: Optional[str] = None):
    rows = _query('''
        SELECT m.*, h.name AS home_team, a.name AS away_team, s.name AS season
        FROM matches m
        JOIN seasons s ON s.season_id = m.season_id
        JOIN teams h ON h.team_id = m.home_team_id
        JOIN teams a ON a.team_id = m.away_team_id
        WHERE m.match_id = %s
    ''', (match_id,), club=club)
    if not rows:
        raise HTTPException(404, f'match {match_id} not ingested')
    return {
        'match': rows[0],
        'teams': _query('SELECT * FROM match_team_stats WHERE match_id = %s',
                        (match_id,), club=club),
        'panels': _query('SELECT panel, file_path, bytes FROM visualizations '
                         'WHERE match_id = %s ORDER BY panel', (match_id,), club=club),
    }


@app.get('/matches/{match_id}/panels')
async def match_panels(match_id: int, club: Optional[str] = None):
    """Panel filenames + dashboard URL for the match gallery."""
    from visualizer import output_layout

    detail = await match_detail(match_id, club=club)
    match = detail['match']
    db_panels = detail['panels']

    dashboard = None
    panel_files: List[str] = []
    match_dir = None
    url_prefix = None

    # Prefer on-disk season layout when present
    season = match.get('season')
    home = match.get('home_team')
    away = match.get('away_team')
    date = str(match.get('match_date') or '')[:10]
    mw = match.get('matchweek')
    if season and home and away:
        season_path = output_layout.season_root(
            season, club=club if club and club != 'gunners' else None)
        expected = output_layout.match_dir_name(date, home, away, mw)
        candidate = Path(season_path) / expected
        if not candidate.is_dir():
            hs, as_ = output_layout.slugify(home), output_layout.slugify(away)
            if Path(season_path).is_dir():
                for name in os.listdir(season_path):
                    if hs in name and as_ in name:
                        candidate = Path(season_path) / name
                        break
        if candidate.is_dir():
            match_dir = str(candidate)
            rel = candidate.relative_to(Path(ROOT) / 'output')
            url_prefix = f'/output/{rel.as_posix()}'
            dashboards = sorted(
                p.name for p in candidate.glob('00_*.png'))
            dashboard = dashboards[0] if dashboards else None
            panels_subdir = candidate / output_layout.PANELS_DIR
            if panels_subdir.is_dir():
                panel_files = sorted(p.name for p in panels_subdir.glob('*.png'))

    if not panel_files and db_panels:
        panel_files = [p['panel'] if p['panel'].endswith('.png')
                       else f"{p['panel']}.png" for p in db_panels]
        for p in db_panels:
            if str(p.get('panel', '')).startswith('00'):
                dashboard = p['panel'] if str(p['panel']).endswith('.png') else f"{p['panel']}.png"
                break

    return {
        'match_id': match_id,
        'match': match,
        'match_dir': match_dir,
        'url_prefix': url_prefix,
        'dashboard': dashboard,
        'panels': panel_files,
        'db_panels': db_panels,
    }


@app.get('/matches/{match_id}/timeline')
async def match_timeline(match_id: int, club: Optional[str] = None):
    """Goals, cards, and substitutions from the event stream."""
    return _query('''
        SELECT e.minute, e.second, e.period, e.type, e.outcome, e.card_type,
               e.is_goal, t.name AS team, p.name AS player
        FROM events e
        LEFT JOIN teams t ON t.team_id = e.team_id
        LEFT JOIN players p ON p.player_id = e.player_id
        WHERE e.match_id = %s
          AND (
            e.is_goal IS TRUE
            OR e.type ILIKE '%%Card%%'
            OR e.type ILIKE '%%Substitution%%'
            OR e.card_type IS NOT NULL
          )
        ORDER BY e.expanded_minute NULLS LAST, e.event_index
    ''', (match_id,), club=club)


@app.get('/matches/{match_id}/shots')
async def match_shots(match_id: int, club: Optional[str] = None):
    return _query('''
        SELECT s.shot_id, s.minute, s.outcome, s.is_goal, s.is_big_chance,
               s.is_penalty, s.x, s.y, s.distance_m, s.xg, s.situation, s.body_part,
               t.name AS team, p.name AS player
        FROM shots s
        JOIN teams t ON t.team_id = s.team_id
        LEFT JOIN players p ON p.player_id = s.player_id
        WHERE s.match_id = %s
        ORDER BY s.minute NULLS LAST, s.shot_id
    ''', (match_id,), club=club)


@app.get('/players/{player_id}/season')
async def player_season(player_id: int, season: Optional[str] = None,
                        club: Optional[str] = None):
    if season:
        rows = _query('''
            SELECT v.*, s.name AS season
            FROM v_player_season_totals v
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.player_id = %s AND s.name = %s
        ''', (player_id, _normalise_season_param(season)), club=club)
    else:
        rows = _query('''
            SELECT v.*, s.name AS season
            FROM v_player_season_totals v
            JOIN seasons s ON s.season_id = v.season_id
            WHERE v.player_id = %s
            ORDER BY s.name DESC
        ''', (player_id,), club=club)
    if not rows:
        raise HTTPException(404, f'no season totals for player {player_id}')
    return rows[0] if len(rows) == 1 else rows


@app.get('/seasons')
async def list_seasons(club: Optional[str] = None):
    return _query(
        'SELECT season_id, name FROM seasons ORDER BY name DESC',
        club=club)


@app.get('/clubs')
async def list_clubs():
    """Provisioned club schemas (search_path targets)."""
    from db.connection import connect
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nspname AS schema
                FROM pg_namespace
                WHERE nspname NOT LIKE 'pg_%%'
                  AND nspname NOT IN ('information_schema', 'public')
                ORDER BY nspname
            """)
            schemas = [r[0] for r in cur.fetchall()]
    # Prefer gunners first when present
    clubs = []
    for s in schemas:
        clubs.append({'club': s, 'schema': s, 'label': 'Arsenal' if s == 'gunners' else s})
    return clubs


# --- static mounts (SPA + rendered panels) ---------------------------------

_output_dir = Path(ROOT) / 'output'
if _output_dir.is_dir():
    app.mount('/output', StaticFiles(directory=str(_output_dir)), name='output')

_spa_dir = Path(ROOT) / 'web' / 'dist'
if _spa_dir.is_dir():
    app.mount('/assets', StaticFiles(directory=str(_spa_dir / 'assets')), name='spa-assets')

    @app.get('/{full_path:path}')
    async def spa_fallback(full_path: str):
        """Serve the Vite build for client-side routes (after API routes)."""
        # Never shadow API/docs/static
        if full_path.startswith((
                'pipeline', 'season', 'matches', 'players', 'seasons', 'clubs',
                'visualizations', 'health', 'docs', 'openapi', 'redoc', 'output',
                'assets', 'api')):
            raise HTTPException(404, 'not found')
        # Root public assets from Vite (`web/public/*` → `web/dist/*`), e.g. cannon.png
        if full_path and '..' not in full_path:
            candidate = (_spa_dir / full_path).resolve()
            try:
                candidate.relative_to(_spa_dir.resolve())
            except ValueError:
                raise HTTPException(404, 'not found') from None
            if candidate.is_file():
                return FileResponse(candidate)
        index = _spa_dir / 'index.html'
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(404, 'SPA not built — run npm run build in web/')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000)
