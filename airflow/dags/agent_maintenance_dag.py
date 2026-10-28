"""Airflow's job in an agentic system: freshness and regression safety.

Airflow does not make the agent intelligent. It keeps the agent's inputs
current and catches it silently getting worse — everything batch, scheduled and
deterministic. What must *not* live here is inference: a user's question needs
a sub-second path and streaming, and Airflow has seconds of per-task overhead
and no way to stream. Questions are served by the API; Airflow maintains what
the API depends on.

Three DAGs:

    agent_documents_refresh   nightly - rebuild derived tables and documents
                              for any match that has been ingested since the
                              last run, and re-embed them
    agent_eval_regression     weekly - run the held-out split and record it,
                              so a slow drift in quality shows up as a number
                              rather than as a complaint
    agent_optimize            manual - recompile under a hard budget and keep
                              the artefact only if the eval improves

**Agent code is not imported here.** Airflow pins SQLAlchemy < 2.0; the analyst
service (Compose `:8100`) holds SQLAlchemy 2 + dspy + fastembed. These DAGs
call that service over HTTP. Heavy imports stay out of module scope so the
scheduler parse stays cheap — see `airflow/DAGS.md`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pendulum
from airflow.decorators import dag, task

ROOT = os.environ.get('GUNNERS_ROOT', '/opt/gunners')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

UK = pendulum.timezone('Europe/London')
SEASON = os.getenv('GUNNERS_SEASON', '2025/2026')
ANALYST_URL = os.getenv('ANALYST_URL', 'http://analyst:8100')

DEFAULTS = {
    'owner': 'gunners',
    'retries': 2,
    'retry_delay': timedelta(minutes=10),
    'depends_on_past': False,
}


def _http_json(method: str, path: str, **kwargs):
    """POST/GET JSON against the analyst service. urllib keeps Airflow light."""
    import json
    import urllib.error
    import urllib.request

    url = f'{ANALYST_URL.rstrip("/")}{path}'
    data = kwargs.pop('json', None)
    body = None if data is None else json.dumps(data).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method=method.upper(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=kwargs.pop('timeout', 600)) as resp:
            raw = resp.read().decode('utf-8')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'{method} {url} -> {exc.code}: {detail}') from exc


# ---------------------------------------------------------------------------
# 1. keep the corpus current
# ---------------------------------------------------------------------------

@dag(dag_id='agent_documents_refresh',
     description='Rebuild tactical tables and semantic documents for new matches',
     schedule='30 4 * * *',                 # nightly, after any fixture DAG
     start_date=datetime(2026, 8, 1, tzinfo=UK),
     catchup=False,
     max_active_runs=1,
     default_args=DEFAULTS,
     tags=['agent', 'maintenance'])
def documents_refresh():

    @task
    def stale_matches() -> list[int]:
        """Matches whose documents are missing or older than their ingest.

        A watermark rather than a full rebuild: regenerating 4,349 documents
        nightly would cost six minutes of embedding for nothing on most days.
        """
        from db.connection import connect

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
            return [r[0] for r in cur.fetchall()]

    @task
    def refresh_via_analyst(match_ids: list[int]) -> dict:
        """Features + documents rebuilt inside the analyst image (SQLAlchemy 2)."""
        if not match_ids:
            print('nothing stale; skipping')
            return {'match_ids': [], 'documents_written': 0}
        result = _http_json(
            'POST', '/maintenance/documents',
            json={'season': SEASON, 'match_ids': match_ids},
            timeout=3600)
        print(result)
        return result

    @task
    def report(result: dict):
        """Fail loudly if the corpus is half-embedded — it retrieves silently
        badly, which is worse than being empty."""
        from db.connection import connect
        with connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT count(*), count(embedding) FROM semantic_documents')
            total, embedded = cur.fetchone()
        print(f'corpus: {total} documents, {embedded} embedded')
        print(f'analyst wrote {result.get("documents_written")} docs')
        if total and embedded != total:
            raise ValueError(f'{total - embedded} documents have no embedding')

    report(refresh_via_analyst(stale_matches()))


# ---------------------------------------------------------------------------
# 2. catch the agent getting worse
# ---------------------------------------------------------------------------

@dag(dag_id='agent_eval_regression',
     description='Weekly held-out evaluation, logged to MLflow',
     schedule='0 6 * * 1',                  # Monday morning
     start_date=datetime(2026, 8, 1, tzinfo=UK),
     catchup=False,
     max_active_runs=1,
     default_args={**DEFAULTS, 'retries': 1},
     tags=['agent', 'eval'])
def eval_regression():

    @task
    def rebuild_dataset() -> int:
        """Regenerate the statistical half against current data.

        Free, exact, and necessary: as new matches land, "how many goals did
        Saka score" has a different right answer, and an eval set that still
        expects the old one would report a regression that is not real.

        Dataset build uses only warehouse SQL + light deps available in Airflow.
        """
        from agent.eval.dataset import build, save
        examples = build(SEASON)
        save(examples)
        return len(examples)

    @task
    def run_eval(_: int) -> dict:
        budget = float(os.getenv('GUNNERS_EVAL_BUDGET', '2'))
        summary = _http_json(
            'POST',
            f'/maintenance/eval?split=test&tier=cheap&budget={budget}',
            timeout=7200)
        print(summary)
        return summary

    @task
    def check(summary: dict):
        """A floor, not a comparison to last week — a hard threshold is easier
        to reason about at 6am than a delta against a run nobody remembers."""
        floor = float(os.getenv('GUNNERS_EVAL_FLOOR', '0.5'))
        score = summary.get('score') or 0
        print(f'score {score} against floor {floor}')
        if score < floor:
            raise ValueError(f'evaluation score {score} is below the floor {floor}')

    check(run_eval(rebuild_dataset()))


# ---------------------------------------------------------------------------
# 3. recompile, and keep it only if it is better
# ---------------------------------------------------------------------------

@dag(dag_id='agent_optimize',
     description='Recompile the DSPy program under a hard budget',
     schedule=None,                          # manual: it spends money
     start_date=datetime(2026, 8, 1, tzinfo=UK),
     catchup=False,
     max_active_runs=1,
     default_args={**DEFAULTS, 'retries': 0},
     tags=['agent', 'optimize'])
def optimize():

    @task
    def compile_program() -> dict:
        optimizer = os.getenv('GUNNERS_OPTIMIZER', 'bootstrap')
        budget = float(os.getenv('GUNNERS_OPTIMIZE_BUDGET', '3'))
        return _http_json(
            'POST',
            f'/maintenance/optimize?optimizer={optimizer}&budget={budget}',
            timeout=7200)

    @task
    def publish(result: dict):
        """Only promote an artefact that actually beat the baseline."""
        if not result.get('improved'):
            print(f'no improvement ({result.get("baseline")} -> '
                  f'{result.get("optimized")}); keeping the current program')
            return
        print(f'promoted {result.get("path")}: {result.get("baseline")} -> '
              f'{result.get("optimized")}')

    publish(compile_program())


documents_refresh()
eval_regression()
optimize()
