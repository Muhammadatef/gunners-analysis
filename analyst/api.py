"""Analyst FastAPI service — chat (SSE) and match reports over HTTP.

Separate from Airflow (SQLAlchemy 2) and from the scraper orchestrator image.
Consumers: the web SPA, and later the paused agent Airflow DAGs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from analyst import conversations as conv
from analyst.stream import chunk_tokens, sse

ROOT = os.environ.get(
    'GUNNERS_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('GUNNERS_ROOT', ROOT)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Configure DSPy once in the startup task — never per-request."""
    from agent import config
    try:
        config.configure(config.DEFAULT_TIER, trace=True)
        logger.info('DSPy configured (tier=%s)', config.DEFAULT_TIER)
    except Exception as exc:  # noqa: BLE001
        logger.warning('DSPy startup configure failed: %s', exc)

    async def _warm() -> None:
        """Load fastembed and the catalogue vectors off the request path.

        The first question otherwise pays an ONNX model load inside its own
        latency budget — several seconds, attributed to the agent, and paid
        again by every fresh container.
        """
        from agent import routing
        try:
            ok = await asyncio.to_thread(routing.warm)
            logger.info('embedding warm-up %s', 'ok' if ok else 'unavailable')
        except Exception as exc:  # noqa: BLE001
            logger.warning('embedding warm-up failed: %s', exc)

    warm_task = asyncio.create_task(_warm())
    try:
        yield
    finally:
        warm_task.cancel()


app = FastAPI(
    title='GunnersAnalysis Analyst',
    description='DSPy football analyst — Q&A and technical match reports',
    version='1.0.0',
    lifespan=lifespan,
)

_origins = os.getenv(
    'ANALYST_CORS_ORIGINS',
    'http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173',
).split(',')
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


class AskRequest(BaseModel):
    question: str
    club: str = 'gunners'
    # gpt-oss (cheap) often fails DSPy JSON fields; default to gpt-4o-mini.
    tier: str = 'fallback'
    season: Optional[str] = '2025/2026'


class ChatRequest(BaseModel):
    question: str
    conversation_id: Optional[str] = None
    club: str = 'gunners'
    # Prefer fallback (gpt-4o-mini) — currently fastest reliable report writer.
    tier: str = 'fallback'
    season: Optional[str] = '2025/2026'
    match_id: Optional[int] = None
    opponent: Optional[str] = None
    intent: Optional[str] = Field(
        default=None,
        description='qa | report — auto-detected when omitted')


class TitleRequest(BaseModel):
    title: str


@app.get('/health')
async def health():
    out: dict[str, Any] = {'status': 'healthy', 'service': 'analyst'}
    try:
        from db.connection import connect
        with connect() as conn:
            conn.cursor().execute('SELECT 1')
        out['postgres'] = 'connected'
    except Exception as exc:  # noqa: BLE001
        out['status'] = 'degraded'
        out['postgres'] = f'unavailable: {exc}'
    out['openrouter'] = 'configured' if os.getenv('OPENROUTER_API_KEY') else 'missing'
    return out


@app.post('/ask')
async def ask(body: AskRequest):
    """Non-streamed Q&A — smoke test and DAG-friendly."""
    from agent import config
    from agent.modules import FootballAnalyst
    from db.connection import club_schema

    if body.tier not in config.TIERS:
        raise HTTPException(400, f'unknown tier {body.tier}')
    schema = club_schema(body.club) if body.club else None

    def _run():
        with config.use_tier(body.tier):
            return FootballAnalyst(schema=schema)(question=body.question)

    pred = await asyncio.to_thread(_run)
    return {
        'answer': pred.answer,
        'tool': pred.tool,
        'route_source': getattr(pred, 'route_source', 'lm'),
        'query_id': pred.query_id,
        'parameters': pred.parameters,
        'tools_used': pred.tools_used,
        'unsupported_numbers': pred.unsupported_numbers,
        'evidence': pred.evidence,
        'reasoning': pred.reasoning,
        'latency_s': pred.latency_s,
        'errors': pred.errors,
    }


class MatchReportRequest(BaseModel):
    match_id: int
    match_dir: str
    season: str = '2026/2027'
    opponent: Optional[str] = None
    club: str = 'gunners'
    tier: str = 'fallback'


@app.post('/report')
async def match_report(body: MatchReportRequest):
    """Panel-grounded technical REPORT.md for one fixture (season DAG).

    Airflow calls this over HTTP so dspy stays out of the Airflow image.
    """
    from agent import config
    from agent.report import MatchReportGenerator, generate_match_report
    from db.connection import club_schema

    if body.tier not in config.TIERS:
        raise HTTPException(400, f'unknown tier {body.tier}')

    schema = club_schema(body.club) if body.club else None
    # Construct Predict on the event-loop thread (owns dspy.settings).
    gen = MatchReportGenerator(schema=schema)
    gen._writer()

    def _run():
        return generate_match_report(
            body.match_id, body.match_dir, body.season,
            opponent=body.opponent, tier=body.tier, schema=schema,
            generator=gen)

    try:
        path = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc
    return {'report_path': path, 'match_id': body.match_id}


@app.get('/conversations')
async def list_conversations(club: str = Query('gunners')):
    return conv.list_conversations(club=club)


@app.post('/conversations')
async def create_conversation(club: str = Query('gunners')):
    cid = conv.create_conversation(club=club)
    return {'id': cid, 'club': club}


@app.get('/conversations/{conversation_id}')
async def get_conversation(conversation_id: str, club: str = Query('gunners')):
    row = conv.get_conversation(conversation_id, club=club)
    if not row:
        raise HTTPException(404, 'conversation not found')
    return row


@app.delete('/conversations/{conversation_id}')
async def delete_conversation(conversation_id: str, club: str = Query('gunners')):
    if not conv.delete_conversation(conversation_id, club=club):
        raise HTTPException(404, 'conversation not found')
    return {'ok': True}


@app.post('/conversations/{conversation_id}/title')
async def title_conversation(conversation_id: str, body: TitleRequest,
                             club: str = Query('gunners')):
    conv.set_title(conversation_id, body.title, club=club)
    return {'ok': True, 'title': body.title}


@app.post('/chat')
async def chat(body: ChatRequest):
    """SSE stream: start → route → evidence → token* → check → done."""

    async def generate() -> AsyncIterator[dict[str, str]]:
        from agent import config
        from agent.modules import FootballAnalyst
        from agent.report import parse_report_slots
        from db.connection import club_schema

        started = time.time()
        club = body.club or 'gunners'
        cid = body.conversation_id or conv.create_conversation(club=club)
        mid = str(uuid.uuid4())
        yield sse('start', {'conversation_id': cid, 'message_id': mid})

        try:
            conv.add_message(cid, 'user', body.question, club=club)
        except Exception:  # noqa: BLE001
            pass

        intent = (body.intent or '').lower().strip()
        # Do not keyword-match here: IntentClassifier (DSPy) decides report vs qa.

        if body.tier not in config.TIERS:
            yield sse('error', {'stage': 'configure', 'message': f'unknown tier {body.tier}',
                                'retryable': False})
            return

        schema = club_schema(club) if club else None

        # What the previous turn was about, so "and against City?" resolves.
        # Explicit request fields win over the carried ones.
        slots: dict = {}
        if body.conversation_id:
            slots = await asyncio.to_thread(
                conv.latest_slots, cid, club)
        for key, value in (('match_id', body.match_id),
                           ('opponent', body.opponent),
                           ('season', body.season)):
            if value:
                slots[key] = value

        # The route is decided once, here, and carried into whichever branch
        # answers. `FootballAnalyst` used to classify a second time on its own,
        # which doubled the LM calls on every question that reached it.
        route = None

        try:
            if not intent:
                from agent.report import (
                    looks_like_report_request_fast,
                    try_parse_report_slots_fast,
                )
                fast_slots = try_parse_report_slots_fast(
                    body.question, body.season or '2025/2026')
                # Skip IntentClassifier when the ask is clearly a report and
                # fixture slots already resolve — that alone was several seconds.
                if (body.match_id
                        or (looks_like_report_request_fast(body.question)
                            and (body.opponent
                                 or (fast_slots or {}).get('opponent')))):
                    intent = 'report'
                    yield sse('route', {
                        'tool': 'report',
                        'query_id': 'match_report',
                        'parameters': {
                            'opponent': body.opponent
                            or (fast_slots or {}).get('opponent'),
                            'season': (fast_slots or {}).get('season')
                            or body.season,
                            'match_id': body.match_id,
                            'matchweek': (fast_slots or {}).get('matchweek'),
                        },
                        'reasoning': (fast_slots or {}).get('reasoning')
                        or 'Match-report request with resolved fixture slots',
                    })
                else:
                    yield sse('route', {
                        'tool': '…',
                        'query_id': '',
                        'parameters': {},
                        'reasoning': 'Routing the question…',
                    })

                    def _classify():
                        from agent.modules import IntentClassifier
                        with config.use_tier(body.tier):
                            return IntentClassifier()(
                                question=body.question, slots=slots,
                                schema=schema)

                    route = await asyncio.to_thread(_classify)
                    intent = 'report' if route.tool == 'report' else 'qa'
                    if route.tool == 'report':
                        yield sse('route', {
                            'tool': 'report',
                            'query_id': 'match_report',
                            'parameters': route.parameters,
                            'reasoning': route.reasoning
                            or 'Classified as a match-report request',
                            'route_source': route.source,
                        })

            if intent == 'report':
                from agent.report import (
                    MatchReportGenerator,
                    render_caveats,
                    render_scoreboard,
                    try_parse_report_slots_fast,
                )

                def _slots():
                    fast = try_parse_report_slots_fast(
                        body.question, body.season or '2025/2026')
                    if fast is None and route is not None and route.parameters:
                        # The classifier already resolved the fixture; do not
                        # pay a second extraction call to learn the same thing.
                        params = route.parameters
                        if params.get('opponent'):
                            fast = {
                                'opponent': params['opponent'],
                                'season': params.get('season')
                                or body.season or '2025/2026',
                                'matchweek': params.get('matchweek'),
                                'home': None,
                                'reasoning': route.reasoning
                                or 'Slots from the routing decision',
                            }
                    if body.opponent or body.match_id or (
                            fast and fast.get('opponent')):
                        base = fast or {
                            'opponent': '',
                            'season': body.season or '2025/2026',
                            'matchweek': None,
                            'home': None,
                            'reasoning': 'Slots from request context',
                        }
                        if body.opponent:
                            base = {**base, 'opponent': body.opponent}
                        return base
                    with config.use_tier(body.tier):
                        return parse_report_slots(
                            body.question, body.season or '2025/2026')

                slots = await asyncio.to_thread(_slots)
                opponent = body.opponent or slots.get('opponent') or ''
                season = slots.get('season') or body.season or '2025/2026'
                matchweek = slots.get('matchweek')
                yield sse('route', {
                    'tool': 'report',
                    'query_id': 'match_report',
                    'parameters': {
                        'opponent': opponent,
                        'season': season,
                        'match_id': body.match_id,
                        'matchweek': matchweek,
                        'home': slots.get('home'),
                    },
                    'reasoning': slots.get('reasoning')
                    or 'Gathering warehouse evidence for the report…',
                })

                gen = MatchReportGenerator(schema=schema)

                def _prepare():
                    return gen.prepare(
                        opponent=opponent, season=season,
                        match_id=body.match_id, matchweek=matchweek,
                        home=slots.get('home'))

                pack = await asyncio.to_thread(_prepare)
                if pack.fixture is None:
                    msg = (
                        f'No evidence was found for that fixture '
                        f'({opponent or "unknown"}, {season}'
                        f'{f", GW{matchweek}" if matchweek else ""}).')
                    for piece in chunk_tokens(msg):
                        yield sse('token', {'text': piece})
                    yield sse('check', {
                        'unsupported_numbers': [],
                        'evidence_numbers': [],
                    })
                    answer = msg
                    meta = {
                        'tool': 'report',
                        'query_id': 'match_report',
                        'parameters': {
                            'opponent': opponent,
                            'season': season,
                            'match_id': body.match_id,
                            'matchweek': matchweek,
                        },
                        'evidence': {'text': pack.evidence_text[:8000]},
                        'evidence_numbers': [],
                        'unsupported_numbers': [],
                        'retrieved': [],
                        'reasoning': 'fixture_not_found',
                        'latency_s': round(time.time() - started, 3),
                    }
                else:
                    yield sse('evidence', {
                        'query_ids': pack.query_ids,
                        'numbers': sorted(pack.numbers),
                        'documents': pack.retrieved,
                        'sources': pack.query_ids,
                        'match_id': pack.match_id,
                        'report_path': None,
                    })

                    # Progressive: scoreboard is instant warehouse MD.
                    scoreboard = render_scoreboard(pack)
                    for piece in chunk_tokens(scoreboard):
                        yield sse('token', {'text': piece})

                    def _analysis():
                        return gen.write_analysis(
                            pack, opponent, season, tier=body.tier)

                    analysis = await asyncio.to_thread(_analysis)
                    if analysis:
                        block = '\n' + analysis.strip() + '\n'
                        for piece in chunk_tokens(block):
                            yield sse('token', {'text': piece})

                    caveats = render_caveats(pack)
                    for piece in chunk_tokens('\n' + caveats):
                        yield sse('token', {'text': piece})

                    from agent.report import _split_analysis_sections
                    sections = _split_analysis_sections(analysis)
                    result = gen.assemble(
                        pack, sections, opponent, season, started,
                        analysis_md=analysis)
                    answer = result.report
                    yield sse('check', {
                        'unsupported_numbers': result.unsupported_numbers,
                        'evidence_numbers': sorted(result.evidence_numbers),
                    })
                    meta = {
                        'tool': 'report',
                        'query_id': 'match_report',
                        'parameters': {
                            'opponent': opponent,
                            'season': season,
                            'match_id': result.match_id,
                            'matchweek': matchweek,
                        },
                        'evidence': {'text': result.evidence[:8000]},
                        'evidence_numbers': sorted(result.evidence_numbers),
                        'unsupported_numbers': result.unsupported_numbers,
                        'retrieved': result.retrieved,
                        'reasoning': slots.get('reasoning') or 'match report',
                        'latency_s': result.latency_s,
                    }
            else:
                def _run_qa():
                    with config.use_tier(body.tier):
                        return FootballAnalyst(schema=schema)(
                            question=body.question, route=route, slots=slots)

                pred = await asyncio.to_thread(_run_qa)
                yield sse('route', {
                    'tool': pred.tool,
                    'query_id': pred.query_id,
                    'parameters': pred.parameters,
                    'reasoning': pred.reasoning,
                    'route_source': getattr(pred, 'route_source', 'lm'),
                })
                yield sse('evidence', {
                    'rows': None,
                    'numbers': sorted(pred.evidence_numbers) if pred.evidence_numbers else [],
                    'documents': pred.retrieved or [],
                    'sources': [pred.query_id] if pred.query_id else [],
                    'text': pred.evidence,
                })
                answer = pred.answer or ''
                for piece in chunk_tokens(answer):
                    yield sse('token', {'text': piece})
                yield sse('check', {
                    'unsupported_numbers': pred.unsupported_numbers or [],
                    'evidence_numbers': sorted(pred.evidence_numbers)
                    if pred.evidence_numbers else [],
                })
                meta = {
                    'tool': pred.tool,
                    'query_id': pred.query_id,
                    'parameters': pred.parameters,
                    'evidence': {'text': pred.evidence},
                    'evidence_numbers': sorted(pred.evidence_numbers)
                    if pred.evidence_numbers else [],
                    'unsupported_numbers': pred.unsupported_numbers,
                    'retrieved': pred.retrieved,
                    'reasoning': pred.reasoning,
                    'latency_s': pred.latency_s,
                }

            try:
                conv.add_message(cid, 'assistant', answer, club=club, **meta)
                if not body.conversation_id:
                    conv.set_title(cid, body.question[:80], club=club)
            except Exception:  # noqa: BLE001
                pass

            yield sse('done', {
                'latency_s': round(time.time() - started, 3),
                'cost_usd': None,
                'tokens': None,
                'mlflow_run_url': os.getenv(
                    'MLFLOW_TRACKING_URI', 'http://localhost:5000'),
                'conversation_id': cid,
                'message_id': mid,
            })
        except Exception as exc:  # noqa: BLE001
            yield sse('error', {
                'stage': 'agent',
                'message': str(exc),
                'retryable': True,
            })

    return EventSourceResponse(generate())


# Maintenance endpoints for Airflow DAGs (HTTP instead of import agent.*)
class RefreshRequest(BaseModel):
    season: str = '2025/2026'
    match_ids: Optional[list[int]] = None


@app.post('/maintenance/documents')
async def maintenance_documents(body: RefreshRequest):
    """Rebuild tactical tables + documents for stale or listed matches."""
    from analytics.build import build as build_features
    from agent.documents.build import build as build_docs
    from db.connection import connect

    match_ids = body.match_ids
    if not match_ids:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT m.match_id
                FROM matches m
                LEFT JOIN (
                    SELECT match_id, max(built_at) AS built_at
                    FROM semantic_documents GROUP BY match_id
                ) d ON d.match_id = m.match_id
                WHERE d.built_at IS NULL OR d.built_at < m.ingested_at
                ORDER BY m.match_date
            """)
            match_ids = [r[0] for r in cur.fetchall()]
    written = 0
    for mid in match_ids:
        build_features(match_id=mid)
        result = build_docs(match_id=mid)
        written += (result or {}).get('written', 0)
    return {'match_ids': match_ids, 'documents_written': written}


@app.post('/maintenance/eval')
async def maintenance_eval(split: str = 'test', tier: str = 'cheap',
                           budget: float = 2.0):
    """Run held-out eval; used by agent_eval_regression over HTTP."""
    from agent import config
    from agent.eval.dataset import load
    from agent.eval.metrics import summarise
    from agent.eval.run import evaluate
    from agent.modules import FootballAnalyst

    def _run():
        conf_budget = config.Budget(limit_usd=budget)
        with config.use_tier(tier) as lm:
            results, stats = evaluate(
                FootballAnalyst(), load(split=split), conf_budget, lm=lm, tier=tier)
            return summarise(results, stats)

    return await asyncio.to_thread(_run)


@app.post('/maintenance/optimize')
async def maintenance_optimize(optimizer: str = 'bootstrap', budget: float = 3.0):
    """Recompile under a hard budget; used by agent_optimize over HTTP."""
    from agent.optimize import run_optimization

    def _run():
        return run_optimization(optimizer=optimizer, budget_usd=budget)

    return await asyncio.to_thread(_run)