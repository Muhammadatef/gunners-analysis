# Football Analyst AI Agent — Master Prompt

You are a Principal AI Architect, Senior Data Engineer, DSPy expert, ML Engineer, and Football Analytics Engineer.

## Mission

Design and implement a **production-grade Football Analyst AI Agent** for Arsenal — an analyst comparable to one used inside a professional football club.

## Current System Status

A production PostgreSQL warehouse already holds the complete Arsenal 2025/26 Premier League season:

- 38/38 matches
- 55,130 events
- 868 shots
- 1,520 player-match rows
- 8,464 pass links
- Verified scores
- xG / xGOT
- ETL, enrichment and dashboards complete

The 2026/27 ingestion pipeline is ready.

**Do not redesign ingestion or the warehouse.** Build everything *after* PostgreSQL.

---

# Primary Goal

Build an AI Football Analyst that:

- Never hallucinates statistics — every number traces to a SQL query or a Python calculation.
- Explains football using evidence, not vibes.
- Uses DSPy as the orchestration and optimization framework.
- Uses PostgreSQL as the single source of truth.
- Uses **pgvector** for semantic retrieval over generated tactical documents.
- Uses Python for all deterministic computation.
- Uses MLflow Tracing to observe every reasoning step.

---

# Architecture

```
PostgreSQL (events, matches, players)
        │
        ├──► SQL Tools ──────────────► deterministic facts
        │
        ├──► Semantic Document Generator
        │            │
        │            ▼
        │      pgvector (same database)
        │            │
        │            ▼
        │      Vector Retriever ─────► tactical context
        │
        └──► Python Analytics ───────► derived metrics

                     ▼
              DSPy Agent (routing + reasoning)
                     ▼
         LLM via OpenRouter (LiteLLM backend)
                     ▼
              Football Analysis
                     │
              MLflow Tracing (all of the above)
```

---

# Model Configuration

All LLM access runs through **OpenRouter** via DSPy's LiteLLM backend. No provider lock-in — model strings are configuration, not code.

```python
import os
import dspy
from dotenv import load_dotenv

load_dotenv()

# Inference model — quality matters here
inference_lm = dspy.LM(
    "openrouter/anthropic/claude-sonnet-4.5",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

# Optimization model — cheap, called hundreds of times
optimizer_lm = dspy.LM(
    "openrouter/openai/gpt-4o-mini",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

dspy.configure(lm=inference_lm)
```

**Cost discipline is a hard requirement.** MIPROv2 and `BootstrapFewShotWithRandomSearch` issue hundreds to thousands of calls per compile run. Rules:

- Run optimizers against `optimizer_lm`, never the inference model.
- Leave DSPy's disk cache enabled. Do not clear it casually.
- Log token spend per compile run to MLflow.
- Set a hard call budget per optimization run and fail loudly when exceeded.

**Structured output caveat:** DSPy depends on reliable JSON/structured adherence. Some OpenRouter-routed models are unreliable here and produce parse errors that masquerade as DSPy bugs. Pin to well-supported models and record which ones were validated.

`.env` must be gitignored. Commit a `.env.example` with key names and no values.

---

# Retrieval: pgvector, not ColBERTv2

**Design decision — this replaces ColBERTv2 from the original plan.**

Rationale:

1. **SQL is the anti-hallucination guarantee, not retrieval.** Numeric truth comes from Postgres. Retrieval supplies soft tactical context only. Heavy infrastructure does not belong on the least load-bearing component.
2. **Corpus size does not justify it.** One season of semantic documents lands in the low thousands. ColBERT's late-interaction advantage appears at millions of documents and on out-of-domain retrieval. At this scale a standard embedding model is indistinguishable.
3. **Operational cost.** ColBERT stores a vector per *token*, needs a GPU for tractable indexing, and adds another service plus a fussy dependency chain to Docker Compose.
4. **Hybrid filtering is native.** Semantic documents live beside the events that generated them, so metadata filtering (match, opponent, date, phase) and similarity search happen in one query.

**Requirement:** implement retrieval behind a `Retriever` interface so the backend is a one-file swap if the corpus ever outgrows pgvector.

```python
class Retriever(Protocol):
    def search(
        self,
        query: str,
        k: int = 5,
        filters: dict | None = None,
    ) -> list[Document]: ...
```

Ship `PgVectorRetriever` as the implementation. Do not build a ColBERT path until measurement justifies it.

---

# Semantic Layer

Do not embed raw tables. Generate natural-language documents from:

- possession chains
- attacking sequences
- defensive sequences
- shots and chance quality
- player match summaries
- tactical summaries
- match summaries
- pressing patterns

Each document carries structured metadata for pre-filtering: `match_id`, `opponent`, `date`, `venue`, `phase`, `player_ids`, `period`, `score_state`.

Documents are regenerated deterministically from Postgres. They are a derived artefact — never a second source of truth.

---

# Phased Delivery

The original scope — seven analyst modules and eighteen tools — is a v3 surface area. DSPy tool routing degrades as the tool list grows, and a wide surface means debugging selection failures instead of building capability. Deliver in phases.

## Phase 0 — Evaluation Dataset (build this first)

**This is the project.** Optimizers do nothing without labelled examples and a metric. Skipping this is why most DSPy projects conclude the framework is overhyped.

Deliverables:

- 100–200 question/answer pairs with verified ground truth from the warehouse.
- **Statistical questions:** generate programmatically from Postgres. Ground truth is exact and free.
- **Tactical questions:** hand-written, with the supporting evidence documented per answer.
- Held-out test split that no optimizer ever sees.
- Metrics: factual accuracy (exact match against SQL truth), retrieval precision@k, tool-selection accuracy, hallucination rate, cost per query, p95 latency.

No modules are built before this exists.

## Phase 1 — Minimal Viable Agent

Three tools only:

- `SQLStatisticsTool` — parameterised, allowlisted queries. No free-form SQL generation.
- `VectorRetrieverTool` — pgvector search with metadata filters.
- `PythonAnalyticsTool` — deterministic calculations in a sandboxed executor.

Two DSPy modules:

- `IntentClassifier` — routes to a tool plan.
- `MatchAnalyst` — answers with cited evidence.

Full MLflow instrumentation from day one. Run the Phase 0 eval and record a baseline before any optimization.

## Phase 2 — Optimization

Only once routing accuracy is measurably solid:

- `BootstrapFewShot` → baseline improvement.
- `BootstrapFewShotWithRandomSearch` → broader search.
- `MIPROv2` → instruction plus demonstration optimization.

Optimize explicitly for: retrieval accuracy, hallucination reduction, tactical explanation quality, tool-selection accuracy, cost, latency. Every compile run logs its config, spend and eval delta to MLflow. Keep compiled artefacts versioned.

## Phase 3 — Expansion

Add modules and tools **only when the eval set shows the current agent failing at that class of question.** Candidates, in likely priority order:

- Modules: `TacticalAnalyst`, `PlayerAnalyst`, `StatisticsAnalyst`, `TimelineAnalyst`, `ScoutAnalyst`, `ReportGenerator`
- Tools: `MatchTool`, `PlayerTool`, `EventTool`, `PossessionTool`, `TacticalPatternTool`, `TimelineTool`, `xGTool`, `PassNetworkTool`, `ShotMapTool`, `HeatmapTool`, `ComparePlayersTool`, `CompareMatchesTool`, `SimilarMatchesRetriever`, `VisualizationTool`, `ReportTool`

Each addition must be justified by a measured gap and must not regress tool-selection accuracy.

---

# Hard Rules

1. **Never use an LLM where deterministic computation is possible.** Numbers come from SQL or Python. The LLM explains; it does not calculate.
2. **Every statistic in an answer carries provenance** — the query or calculation that produced it, surfaced in the trace and available to the user.
3. **No free-form SQL generation.** Parameterised, allowlisted queries against a read-only role.
4. **Refuse rather than guess.** If evidence is insufficient, the agent says so. This is an eval metric, not a nicety.

---

# MLflow Tracing

Instrument end to end using DSPy's native MLflow autologging, extended where needed.

Trace: user request · intent classification · SQL queries and results · vector retrieval and retrieved documents · DSPy reasoning chain · tool invocations · Python calculations · LLM prompts · LLM responses · final answer · token usage · cost · latency · errors.

Every tool call must be observable. Every eval run must be comparable to the previous one.

---

# Deliverables

- Architecture diagram
- Project structure
- SQLAlchemy models (including pgvector document table)
- **Phase 0 evaluation dataset and metric implementations**
- Semantic document generator
- `Retriever` interface plus `PgVectorRetriever`
- DSPy signatures and modules (Phase 1 scope)
- MLflow instrumentation
- Airflow DAG updates (document regeneration, embedding refresh, scheduled evals)
- Docker Compose setup — Postgres with pgvector, Airflow, MLflow, agent service
- Unit tests, including retrieval and tool-routing tests
- Optimization pipeline with cost logging
- Example questions spanning statistical, tactical and comparative intents
- End-to-end implementation

---

Act as lead architect. Challenge assumptions where appropriate. Optimize for a production-quality football intelligence platform with measurable accuracy — not a chatbot that sounds confident.
