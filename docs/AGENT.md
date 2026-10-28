# The Football Analyst AI Agent

A question-answering layer over the warehouse that is designed so it *cannot*
invent a statistic. Built to `Football_Analyst_AI_Agent_Master_Prompt.md`.

```
PostgreSQL (events, matches, players)
    ├── SQL tools ─────────── deterministic facts
    ├── semantic documents ── pgvector ── tactical context
    └── Python analytics ──── derived metrics
                 ↓
          DSPy agent (routing + reasoning)
                 ↓
       LLM via OpenRouter (LiteLLM)
                 ↓
   answer + provenance, traced in MLflow
```

---

## Why it does not hallucinate statistics

This is an architecture, not an instruction in a prompt. Three mechanisms, all
mechanical:

**The model never writes SQL.** It chooses an id from
`agent/tools/sql_registry.py` and supplies parameters, which psycopg2 binds.
The worst mistake available to it is picking the *wrong* query — a routing
error, which the eval measures and which produces a visibly odd answer. It
cannot produce a number that no query returned.

**The model never does arithmetic.** Percentages, ratios and differences go
through `agent/tools/python_tool.py`, which walks the expression's AST rather
than calling `eval`. Only numbers, arithmetic operators and a handful of
whitelisted functions exist to it — no names, no attributes, no imports.

**Every number is checked before the answer is returned.** Each tool result
carries an `Evidence` object holding the query id, its parameters and the
values. `FootballAnalyst.forward` extracts every number from the drafted answer
and compares it against the set the evidence vouched for. Anything unmatched is
reported in `unsupported_numbers`, and it is exactly what the hallucination-rate
metric counts.

**What this does not buy.** The *interpretation* can still be wrong. "Arsenal
pressed higher in the second half" is a judgement; the figures under it are
verifiable, the causal claim is not. Retrieval can also surface irrelevant
context and yield an answer where every number is true and the conclusion
misleads. That is why retrieval precision and factual accuracy are measured
separately.

The fourth rule is **refuse rather than guess**. If no tool returns evidence,
the agent says so — and the eval set contains questions that can only be
answered by refusing (injuries, transfer fees, other clubs' players, next
season, per-shot xG), scored as their own metric. An agent that answers
everything is not trustworthy.

---

## Layout

| Path | What it is |
|---|---|
| `analytics/` | derived tactical tables: possessions, pressing, carries |
| `agent/documents/` | semantic document generators + the corpus builder |
| `agent/tools/sql_registry.py` | **the allowlist** — every permitted query |
| `agent/tools/retriever.py` | `Retriever` protocol + `PgVectorRetriever` |
| `agent/tools/python_tool.py` | AST-walking calculator |
| `agent/modules.py` | DSPy signatures, `IntentClassifier`, `MatchAnalyst` |
| `agent/report.py` | Panel-grounded match `REPORT.md` (`generate_match_report`) |
| `agent/eval/` | Phase 0 dataset, metrics, runner |
| `agent/optimize.py` | Phase 2 optimizers, under a budget |
| `agent/config.py` | model tiers, spend guard, MLflow |
| `db/schema_agent.sql` | analyst-layer DDL, separate from the warehouse |

---

## The data underneath

Four derived tables, because Opta does not record any of this:

- **`possession_sequences`** (11,182) — Opta emits no possession id. A sequence
  ends only when the opposition win *controlled* possession; their blocks and
  deflections inside our attack belong to our sequence. The naive rule collapses
  most attacks to one event.
- **`pressure_events`** (11,585) and PPDA — no pressure event exists either, so
  pressing is reconstructed from defensive actions using the cookbook's formula.
- **`carries`** (12,026) — the metres driven between the pass that arrives and
  the pass that leaves, which the feed never shows.
- **`match_tactical_features`** (76) — PPDA, counterpressing, sequence shape,
  directness, field tilt.

Then **4,349 semantic documents** across six types (sequence, player, shot,
tactical, pressing, match), embedded with fastembed
(`BAAI/bge-small-en-v1.5`, 384-dim, ONNX — no torch) into pgvector.

Documents are prose, not dumped numbers: a row of figures shares no vocabulary
with the question a person asks. They are a derived artefact and never a second
source of truth — drop the table and a rebuild reproduces it exactly.

---

## Model tiers, and money

One OpenRouter key, three tiers. Which one you are on matters more than
anything else in the configuration.

| Tier | Model | $ /1M in/out | For |
|---|---|---|---|
| `wiring` | `openai/gpt-oss-20b:free` | **0 / 0** | plumbing and smoke tests |
| `cheap` | `openai/gpt-oss-20b` | 0.03 / 0.13 | every optimizer run |
| `quality` | `anthropic/claude-sonnet-4.5` | 3 / 15 | final answers |

Prove the wiring on the free tier. Whether retrieval returns documents and
whether routing picks the right tool are properties of the wiring, not of model
quality, and debugging them should cost nothing.

Two departures from the master prompt, both in its spirit: it pins the
optimizer to `gpt-4o-mini`, but the paid `gpt-oss-20b` is five times cheaper
for comparable bootstrapping (`gpt-4o-mini` remains the documented fallback if
structured output proves unreliable — the spec's own caveat); and plumbing runs
free.

**The spend guard is a hard stop.** `config.Budget.charge` raises
`BudgetExceeded` before a call that would cross the ceiling, and nothing
catches it. A half-finished optimization is recoverable; a drained account is
not.

---

## Running it

```bash
make agent-setup                    # tactical tables, documents, eval set, gates
make eval SPLIT=dev                 # baseline, on the free tier
make ask Q="Was Arsenal a high-pressing side in 2025/26?"
make optimize OPTIMIZER=bootstrap BUDGET=2
make eval SPLIT=test TIER=quality   # the held-out split, once
```

MLflow is at `http://localhost:5000`. The tracking server image in
`docker-compose.yml` must stay on the same major as the host client
(`agent/requirements.txt`: `mlflow>=3`). A 2.x UI against 3.x traces fails open
with `can't access property "span_id", e.context is undefined`. After changing
the image, hard-refresh the browser (Ctrl+Shift+R) and open a **new** ask run —
traces written while the UI was on 2.x may still be unreadable.

---

## Evaluation

The spec calls Phase 0 "the project", and it is right: an optimizer is a search
guided by a score, and with no score there is nothing to search.

**130 examples, 18 families.** The statistical half is *generated* — each
question is a template instantiated against real rows, and its ground truth
comes from running the same allowlisted query the agent would run. Exact, free,
and regenerable after every new match without a human or a token. The tactical
half is hand-written, because no SQL produces "did Arsenal press higher than
City".

**Split by family, not by row** — 74 train / 18 dev / 38 test. A random split
would put "how many goals did Saka score" in train and the same question about
Rice in test, and a model that memorised one would score well on the other for
no good reason. The refusal family is held out too: optimizing on refusals
teaches the shape of the excuse.

Six metrics: factual accuracy, retrieval precision@k, tool-selection accuracy,
hallucination rate, cost per query, p95 latency — plus refusal accuracy scored
separately, because it is invisible inside an average.

---

## Airflow's role

Airflow gives the agent **freshness and regression safety, not intelligence**.

| DAG | Schedule | Does |
|---|---|---|
| `agent_documents_refresh` | nightly | rebuild tables and documents for matches ingested since the last run |
| `agent_eval_regression` | Mondays | run the held-out split, log to MLflow, fail below a floor |
| `agent_optimize` | manual | recompile under a budget; promote only on improvement |

**Interactive inference does not belong in a DAG.** A user's question needs a
fast path and streaming; Airflow has seconds of per-task overhead and neither.
It maintains what the answer path depends on.

**Exception — batch match reports:** each fixture DAG ends with
`football_analyst_DSPy_agent_match_report`, which POSTs to the analyst service
(`POST /report`) so a panel-grounded `REPORT.md` is written beside the dashboard
once the warehouse row exists. That is one offline LM call per match, not chat.
Heavy imports stay in the analyst container; Airflow only does HTTP.

One repo-specific rule: heavy imports (`dspy`, `mlflow`, `fastembed`) go
**inside `@task` bodies**. The scheduler re-parses every DAG file on a loop and
there are 39 others; a model load at module scope would be paid on every parse.

**These three DAGs do not run today.** None of dspy, optuna, pgvector,
sqlalchemy, fastembed or mlflow is installed in the Airflow image, and the cost
of the import rule above is that this does not break DAG parsing — it breaks
the task, unattended, at 04:30. All three are paused.

Installing them there is the obvious fix and it is a trap: `agent/models.py`
needs SQLAlchemy ≥ 2.0, Airflow 2.10.4 pins < 2.0, and pip resolves that by
installing 2.0.51 with a *warning* and a successful build. The image then
imports `airflow` fine and cannot load `airflow.models.TaskInstance`, which
takes down all 39 fixture DAGs to gain three. The two versions need separate
interpreters — see STATUS.md open item 9. Until then the analyst is host-only
(`LOCAL=1 make ask`) and the corpus is refreshed by hand.

---

## Known limits

- **Per-shot xG does not exist.** `shots.xg` is NULL throughout — match-level xG
  comes from SofaScore and no shot-level model has been built. Questions of the
  form "which chance was best" are in the eval set as things to *refuse*.
- **Only Arsenal's matches are in the warehouse.** Opposition players appear
  solely in matches against Arsenal, so cross-club comparisons are refusals.
- **Retrieval matches words, not magnitudes.** "A long passing move" will not
  reliably return the longest — that is what `longest_sequences` is for, and why
  numeric questions route to SQL.
- Phase 3 (the other six modules and fifteen tools) is deliberately unbuilt. The
  spec says add them when the eval shows a measured gap, not before.
