# GunnersAnalysis Web App — Master Prompt

You are a Principal Frontend Architect, Product Designer and Backend Engineer.

Build the **web surface** for GunnersAnalysis: a football intelligence platform that
already has a working data pipeline, a populated warehouse and a DSPy analyst agent.

**Do not redesign the pipeline, the warehouse or the agent.** They exist, they are
tested, and they are documented in `docs/ARCHITECTURE.md`, `docs/DATABASE.md` and
`docs/AGENT.md`. Build everything *after* them.

---

## 1. What exists today

| Layer | State | Where |
|---|---|---|
| Event pipeline | 7 stages, 530 unit tests | `orchestrator/pipeline.py` |
| Warehouse | 14 tables, 4 views, one Postgres schema per club | `db/schema.sql` |
| Data | 38/38 matches, 55,130 events, 868 shots, 1,520 player-match rows | schema `gunners` |
| Rendered reports | 38 dashboards + 722 panels, 338 MB | `output/2025-2026/` |
| Analyst agent | DSPy: 2 signatures, 3 modules, 17 allowlisted queries, 4,349-doc pgvector corpus | `agent/` |
| Evaluation | 130 questions, 18 families, held-out split, MLflow-logged | `agent/eval/` |
| HTTP API | FastAPI, 7 endpoints, Redis job status | `orchestrator/api.py` |
| Tracing | MLflow, every reasoning step | `localhost:5000` |

There is **no UI**. Everything is `make` targets, `psql` and PNG files on disk.
That is the gap this brief closes.

---

## 2. The five constraints that shape the design

These are not preferences. Each one is a measured property of the running system,
and a design that ignores any of them will be wrong in a way that is expensive to
undo. Read all five before proposing an architecture.

### 2.1 The agent cannot live in the Airflow image

`agent/models.py` needs **SQLAlchemy ≥ 2.0**; Airflow 2.10.4 pins **< 2.0**. pip
resolves the conflict by installing 2.0.51, printing a warning and exiting 0 — the
image builds clean, imports `airflow` fine, and then cannot load
`airflow.models.TaskInstance`. This was built and measured; see STATUS.md open
item 9. The three agent DAGs are paused for this reason.

**The orchestrator image has no Airflow in it, so it has no such conflict.**

This gives the backend its shape, and the shape solves two problems at once:

> **Build a dedicated `analyst` service.** One FastAPI process, built from
> `agent/requirements.txt`, holding dspy + SQLAlchemy 2 + pgvector + fastembed +
> mlflow. The web app calls it. **And so do the three blocked Airflow DAGs** —
> over HTTP instead of by importing `agent.*`, which unblocks them without
> touching Airflow's dependency tree.

Do not solve the chat backend in a way that leaves the DAG problem unsolved. One
service, two consumers.

### 2.2 An answer takes tens of seconds

Measured: **27.5 s** for a simple SQL-routed question on the free tier. The path is
classify → query → answer, two sequential LLM calls plus a database round trip.

A spinner for half a minute is a broken product. The transport must be **streaming**
(SSE), and the UI must show the *stages* as they resolve — "reading the question",
"running `season_summary`", "writing" — because the intermediate steps are
genuinely interesting here, not filler. The user should see the query id and its
parameters land **before** the prose starts.

### 2.3 `unsupported_numbers` is the product

Every prediction returns this field. It holds any figure in the answer that the
evidence did not vouch for. This is the platform's entire claim to trustworthiness
and it must be a **first-class visual state**, never a debug detail:

- Empty → a quiet, earned confirmation. Not a green badge shouting VERIFIED.
- Non-empty → the specific numbers are marked **in the answer text itself**, and
  the answer is visibly qualified.

If you build a chat UI that hides this, you have built a worse ChatGPT.

### 2.4 A refusal is a correct answer

The agent is designed to refuse: injuries, transfer fees, other clubs' players,
anything in the future, per-shot xG (`shots.xg` is NULL throughout). The evaluation
set scores refusal accuracy as its own metric.

Render a refusal as a **legitimate, complete answer** — never as an error state,
never in red, never with a retry button as the primary action. Offer what the
system *can* answer instead.

### 2.5 One club is one Postgres schema

Nothing in the codebase writes schema-qualified SQL; the schema is selected per
connection via `search_path`. Every read endpoint already accepts `?club=`.
The frontend must carry a club context through every request and every route.
Arsenal is `gunners`; anything else is its own clone.

---

## 3. Architecture

### 3.1 Recommended

```
┌──────────────────────────────────────────────────────────┐
│  web/          Vite + React + TypeScript SPA             │
│                built to static files                     │
└───────────────┬──────────────────────────┬───────────────┘
                │                          │
      ┌─────────▼──────────┐    ┌──────────▼─────────────┐
      │ orchestrator :8000 │    │ analyst :8100  ★ NEW   │
      │ FastAPI (exists)   │    │ FastAPI                │
      │ · season + match   │    │ · POST /chat  (SSE)    │
      │   reads            │    │ · conversation store   │
      │ · pipeline runs    │    │ · agent/ imported here │
      │ · serves the SPA   │    │ · SQLAlchemy 2 safe    │
      │ · serves output/   │    └──────────┬─────────────┘
      └─────────┬──────────┘               │
                │        ┌─────────────────┤
                ▼        ▼                 ▼
        ┌──────────────────┐        ┌─────────────┐
        │ Postgres+pgvector│        │ MLflow :5000│
        └──────────────────┘        └─────────────┘
                ▲
                │  HTTP, not `import agent`
        ┌───────┴────────┐
        │ Airflow :8080  │  ← unblocks the 3 paused agent DAGs
        └────────────────┘
```

**Why this and not the alternatives:**

- **The SPA is static.** `orchestrator` already runs uvicorn; mount the build output
  and the `output/` tree from it. No Node process in production, no extra container,
  no SSR to reason about. This repo's character is few services, each explicable.
- **The analyst is separate** because of §2.1, because `fastembed` is a few hundred
  MB of ONNX Runtime that the scraper image should not carry, and because it lets
  the agent restart without interrupting a scrape.
- **Reads stay on the orchestrator** because `_query()` and the four views are
  already there and already club-aware.

### 3.2 Alternatives, honestly

| Option | Take it when | Cost |
|---|---|---|
| **Next.js (App Router)** | You want SSR, SEO, or this becomes multi-tenant and public | A Node service in Compose; server/client boundary to reason about; overkill for a single-operator tool behind localhost |
| **SvelteKit** | You want the smallest bundle and enjoy Svelte | Smaller ecosystem for the chart and chat primitives you will want |
| **Streamlit / Gradio** | You need something usable this afternoon | Cannot deliver the chat or the design in this brief; treat as a throwaway, not a foundation |
| **Put the agent in the orchestrator image** | You want one fewer service | Works (no Airflow conflict there), but bloats the scraper image and couples agent restarts to pipeline availability |
| **Chat over WebSocket, not SSE** | You later add multi-user presence or interruption mid-token | More moving parts; SSE is the right size for one-way token streaming and survives proxies better |

**Recommendation: the Vite SPA + separate `analyst` service.** Revisit only if the
app needs to be public.

---

## 4. Backend specification

### 4.1 New service: `analyst`

```
analyst/
  Dockerfile          from python:3.13-slim + agent/requirements.txt
  api.py              FastAPI app
  conversations.py    persistence
  stream.py           SSE framing
```

Compose service on `:8100`, `depends_on: postgres`, bind-mounts the repo, and
**must not** be given Airflow.

### 4.2 Chat endpoints

```
POST   /chat                      → SSE stream (see §4.3)
GET    /conversations             → list, newest first
POST   /conversations             → create, returns id
GET    /conversations/{id}        → full message history with evidence
DELETE /conversations/{id}
POST   /conversations/{id}/title  → auto-title from the first question
GET    /health
```

`POST /chat` body:

```json
{
  "conversation_id": "uuid | null",
  "question": "Was Arsenal a high-pressing side in 2025/26?",
  "club": "gunners",
  "tier": "cheap",
  "season": "2025/2026"
}
```

### 4.3 The streaming contract

Named SSE events, in this order. This maps directly onto what
`FootballAnalyst.forward` already computes — do not invent stages the agent does
not have.

| Event | Payload | UI shows |
|---|---|---|
| `start` | `{conversation_id, message_id}` | message bubble appears |
| `route` | `{tool, query_id, parameters, reasoning}` | "Running `season_summary`" + the parameter chips |
| `evidence` | `{rows, numbers, documents[], sources[]}` | evidence drawer populates, collapsed |
| `token` | `{text}` | prose streams in |
| `check` | `{unsupported_numbers[], evidence_numbers[]}` | verification state resolves |
| `done` | `{latency_s, cost_usd, tokens, mlflow_run_url}` | footer meta |
| `error` | `{stage, message, retryable}` | inline, with the stage named |

Two rules:

- **`check` arrives after the last `token`.** The number check runs on the finished
  answer. Do not render a verification state before it exists — show "checking" and
  resolve.
- **A `route` of `tool: "none"` is a refusal**, not an error. It still streams
  tokens and still ends with `done`.

### 4.4 Reuse, do not rebuild

| Need | Already exists |
|---|---|
| Run the agent | `agent.modules.FootballAnalyst` — returns everything above in one `Prediction` |
| Allowlisted queries | `agent.tools.sql_registry` — 17 queries, `describe_all()`, `run()` |
| Retrieval | `agent.tools.retriever.PgVectorRetriever` |
| Number check | `agent.eval.metrics.hallucinated_numbers` |
| Model tiers + spend guard | `agent.config` — `Budget` raises before it overspends. **Keep it.** |
| Club → schema | `db.connection.club_schema`, `use_schema` |
| Season reads | `orchestrator.api._query` + the 4 views |
| Match directory for a job | `visualizer.output_layout.find_match_dir`, `match_panels` |

### 4.5 Extend the orchestrator with these reads

The season and match pages need data the API does not yet expose. Add to
`orchestrator/api.py`, all club-aware, all reusing `_query`:

```
GET /matches?club=&season=&competition=      list, newest first, for the match grid
GET /matches/{id}/panels                     panel filenames + the dashboard
GET /matches/{id}/timeline                   goals, cards, subs from events
GET /matches/{id}/shots                      shot map data (x, y, outcome, player)
GET /players/{id}/season?season=             one player's per-90s
GET /seasons?club=                           what seasons exist
GET /clubs                                   which schemas are provisioned
```

Serve the rendered PNGs as static files:

```python
app.mount('/output', StaticFiles(directory=ROOT / 'output'), name='output')
```

### 4.6 Persistence

Conversations go in the **`gunners` schema** (or the active club's), not SQLite,
not a JSON file — the warehouse is already the source of truth and already backed
up. Two tables in `db/schema_agent.sql`:

```sql
CREATE TABLE conversations (
  id           UUID PRIMARY KEY,
  club         TEXT NOT NULL,
  title        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id                  UUID PRIMARY KEY,
  conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role                TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content             TEXT NOT NULL,
  tool                TEXT,
  query_id            TEXT,
  parameters          JSONB,
  evidence            JSONB,
  evidence_numbers    NUMERIC[],
  unsupported_numbers NUMERIC[],
  retrieved           JSONB,
  reasoning           TEXT,
  latency_s           NUMERIC,
  cost_usd            NUMERIC,
  mlflow_run_id       TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON messages (conversation_id, created_at);
```

Persisting `evidence` and `unsupported_numbers` means a reopened conversation shows
its provenance, not just its prose. That is the difference between a transcript and
a record.

### 4.7 Security — say the quiet part

Today: no auth, `POSTGRES_PASSWORD=gunners`, `AIRFLOW_PASSWORD=admin`, and `.env`
sits next to `.env.example`. That is survivable on `localhost` and unacceptable the
moment a port is exposed.

Required before this app leaves the machine:

- The analyst service connects as a **read-only Postgres role**. The SQL registry is
  an allowlist, but defence in depth is free here.
- Rate-limit `POST /chat` per session. The spend guard protects the wallet; a rate
  limit protects the guard.
- CORS locked to the app's own origin.
- Rotate the defaults; keep `.env` gitignored.
- If it goes on a network: auth in front of everything, including MLflow, which
  will happily show anyone your prompts.

---

## 5. Design system

### 5.1 Palette — "Chalk & Pitch" (recommended)

Grounded in the pitch and its markings, not the club. Arsenal red (`#EF0107`) is
the obvious accent and the wrong one: `visualizer/palette.py` exists precisely
*because* that red collides with six other clubs' kits, and an interface accent
that also means "a team" is an accent that will lie to you on a match page.

Reserve club colour for data. Take the interface accent from the broadcast
overlay: a deep teal that sits analogous to the green ground and never competes
with a team.

```css
:root {
  --ground:      #E7EAE4;   /* chalk, green-biased — not a default grey */
  --surface:     #F1F3EE;
  --surface-2:   #DDE2DA;
  --ink:         #131B17;
  --ink-soft:    #33413A;
  --muted:       #5B6862;
  --rule:        #C6CDC3;
  --accent:      #0B6E63;
  --accent-hover:#0A5A51;
  --accent-soft: #D3E4E0;
  --ok:   #3C7A46;  --ok-bg:   #DCE8DC;
  --warn: #96690F;  --warn-bg: #EFE4CB;
  --stop: #9E3A2C;  --stop-bg: #F0DBD6;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:      #0D1311;
    --surface:     #151D19;
    --surface-2:   #1E2823;
    --ink:         #DDE4DE;
    --ink-soft:    #B6C1BA;
    --muted:       #8A968F;
    --rule:        #2A3833;
    --accent:      #33D6C0;
    --accent-hover:#5FE3D1;
    --accent-soft: #16302B;
    --ok:   #7CBE85;  --ok-bg:   #17281A;
    --warn: #D6A954;  --warn-bg: #2E2413;
    --stop: #E08774;  --stop-bg: #301A16;
  }
}

:root[data-theme="dark"] { /* repeat the dark block so an explicit toggle wins */ }
```

**Three theme states, not two.** An explicit choice stamps
`data-theme="dark"|"light"` on the root; the default "system" setting stamps
nothing, and only `prefers-color-scheme` separates the two. Define the complete
light palette on bare `:root`, redefine **only tokens** in the media query guarded
as `:root:not([data-theme="light"])`, and redefine them again under
`:root[data-theme="dark"]`. Never give a colour its only definition inside a media
or `[data-theme]` block.

**Data colour is separate from all of this.** Home and away come from
`visualizer/palette.py` via `match.json`; sequential scales (xT, danger zones) use a
perceptually uniform ramp. Semantic ok/warn/stop is a third set and is never the
accent.

### 5.2 Alternatives

- **"Broadcast Night"** — dark only. `#0A0A0C` ground, electric lime `#C7F04A`
  accent, heavy condensed type. Reads like a Champions League overlay. High energy,
  genuinely distinctive, and a real risk on a page dense with small numbers; the
  lime must never touch body text.
- **"Matchday Programme"** — warm paper, red ink, a serif display, generous rules.
  Handsome and on-theme for a *report*, wrong for a tool you operate daily.
  Be aware this lands close to a very common generated look (cream + serif +
  terracotta); if you take it, push the paper cooler and the accent away from
  terracotta, or it will read as templated.

Pick one and commit. Do not blend them.

### 5.3 Typography

Self-host every face as woff2 under `web/public/fonts/`. **Never link a font CDN** —
one blocked request and the whole page silently falls back.

| Role | Face | Use |
|---|---|---|
| Display | **Archivo** (600–800), tight tracking | page titles, scorelines, big figures |
| Body | **Public Sans** (400/500/600) | everything read as prose, including chat |
| Data | **JetBrains Mono** (400/500) | query ids, parameters, panel names, eyebrows |

Rules: body text near 65–75 characters; `font-variant-numeric: tabular-nums` on
every column of digits and every scoreline; uppercase eyebrow labels get
`0.14em` letter-spacing; headings get `text-wrap: balance`.

### 5.4 Layout and motion

- 8px spacing scale; layout with flex/grid `gap`, never per-element margins.
- Radii: 4px controls, 6px cards, 100px pills. No `rounded-2xl` on everything.
- Wide content — tables, panel rows, code — scrolls inside its own
  `overflow-x: auto`. The page body never scrolls sideways.
- Motion is for state changes only: streamed tokens, drawer open, tab change.
  150–200ms, ease-out. Respect `prefers-reduced-motion`. No ambient animation, no
  scroll-triggered reveals — this is an instrument, not a landing page.
- Visible `:focus-visible` on everything interactive. Full keyboard operation.

---

## 6. Frontend

### 6.1 Routes

```
/                          Season overview — the default landing
/matches                   All matches, filterable
/matches/:id               One match: header, tabs, panels        ← §6.3
/matches/:id/:tab          Deep-linkable tab
/chat                      The analyst                            ← §6.4
/chat/:conversationId
/players                   Season per-90s, sortable
/players/:id               One player
/pipeline                  Run a match from two links; job status
/system                    Health, DAG state, readiness gates, eval history
```

Club is a global context (header switcher), persisted, and appended as `?club=` to
every request.

### 6.2 Season overview

Above the fold, in this order: **league position and points**, form over the last
six, then goals and xG for/against. Below: the fixture list with results, and the
season's tactical signature — PPDA, field tilt, possessions ending in a shot —
each with the interpretive scale beside it, because "PPDA 10.76" is meaningless
without knowing that elite pressing is 7–9 and passive is 15+. A number without its
scale is decoration.

Sources: `v_team_season_summary`, `v_season_progress`, `match_tactical_features`.

### 6.3 The match page — panels

This is the centrepiece. `GET /matches/:id/panels` returns the filenames;
`match.json` carries the metadata.

**Header.** Both crests, the scoreline in display type, competition and matchweek,
date and venue. Directly beneath, a four-figure strip — **xG · possession · shots ·
xT** — each rendered as an opposed bar so the two sides read as a comparison, not
two numbers. Team colours from `match.json`, never from your palette.

**The panels have a structure — use it.** They are not eighteen items. They are
**six rows of three: home | shared | away.**

| Row | Home | Shared | Away |
|---|---|---|---|
| 1 | `01` pass network | `02` shot map | `03` pass network |
| 2 | `04` defensive block | `05` GK performance | `06` defensive block |
| 3 | `07` progressive passes | `08` match momentum | `09` progressive passes |
| 4 | `10` danger zones | `11` match stats | `12` danger zones |
| 5 | `13` pass end zones | `14` high turnovers | `15` pass end zones |
| 6 | `16` chance creating | `17` crosses | `18` chance creating |

The odd/even numbering *is* the mirror. A three-column layout where the shared
panel sits between the two sides makes every row read as a direct comparison and
costs nothing. A flat responsive grid throws that away — do not.

**Tabs**, grouping those rows by question rather than by file order:

| Tab | Contains | Answers |
|---|---|---|
| **Dashboard** | `00_dashboard` full-bleed | the one-image summary |
| **Build-up** | rows 1a/1c, 3a/3c, 5a/5c | how did they get the ball forward |
| **Attack** | `02`, `16`/`18`, `17` | where did the chances come from |
| **Defence** | `04`/`06`, `10`/`12`, `14` | how did they defend and press |
| **Context** | `08`, `11`, `05` | how did the match actually go |
| **Data** | timeline, shot table, player table | the numbers underneath |

Panels are large PNGs. Lazy-load below the fold, show an aspect-ratio skeleton so
nothing reflows, click to open full-screen with arrow-key navigation, and offer a
download. On mobile the three columns stack **home → shared → away**, keeping the
reading order.

**"Ask about this match"** button in the header, opening `/chat` pre-seeded with
the match context. This is the join between the two halves of the product; without
it they are two apps sharing a header.

### 6.4 The chat — the analyst

Take the shape from Claude and ChatGPT, because it is the shape people already
know. Then add the three things that make this different from a chatbot.

**The familiar shape:**

- Left rail: conversations grouped by day, newest first, rename and delete,
  collapsible. Hidden by default under 900px.
- Centre column, `max-width: 48rem`, generous line-height. User messages in a
  tinted bubble aligned right; assistant messages **unbubbled** on the page ground —
  the asymmetry is what makes long answers readable.
- Composer pinned to the bottom, auto-growing to ~8 lines, ⌘/Ctrl+Enter to send,
  Shift+Enter for newline. A **Stop** button while streaming, in the same position
  as Send.
- Autoscroll that yields the moment the user scrolls up, with a "jump to latest"
  affordance.
- Markdown, tables and code blocks in answers. Copy button per message.
- An empty state with **four real starter questions** taken from the eval families,
  so the first interaction succeeds:
  - "Was Arsenal a high-pressing side in 2025/26?"
  - "How many points did Arsenal take in 2025/26?"
  - "Who created the most chances against Manchester City?"
  - "Which chance was Arsenal's best against Liverpool?" *(this one is refused —
    include it deliberately, so the refusal behaviour is discovered early rather
    than mistaken for a bug)*

**The three additions that are the point:**

**1 · The reasoning strip.** While `route` and `evidence` stream, show a compact
strip above the answer: the tool chosen, the query id in mono, the bound parameters
as chips. It collapses to a single line once tokens start. This is not a loading
animation — it is the provenance, and it is the reason to trust what follows.

```
┌─────────────────────────────────────────────────────┐
│ ● sql   season_summary   team: Arsenal              │
│                          season: 2025/2026          │
└─────────────────────────────────────────────────────┘
```

**2 · The evidence drawer.** Collapsed under every assistant message: the exact
rows returned, or the retrieved documents with their match, opponent and date
metadata, and the MLflow run link. One click from any claim to the thing that
produced it.

**3 · The verification state.** After `check`:

- `unsupported_numbers` empty → a single quiet line beneath the answer:
  *"Every figure traces to the evidence."* Muted, small, no badge, no green tick.
  Understatement reads as confidence.
- Non-empty → those exact numerals are marked **inline in the answer text** with a
  warn-toned underline, and the line reads
  *"2 figures are not in the evidence: 71, 0.63."* The answer stays visible. Never
  hide it; the user needs to see what the system got wrong.

**Refusals** render in the normal answer style with no error affordance, followed
by what *can* be answered — e.g. "Per-shot xG is not in the warehouse. I can show
you the shot map, or every shot's outcome and location."

**Latency.** Stage labels while waiting, never a bare spinner. Show elapsed seconds
after 10s. Surface the tier in the composer, with the cost implication in a
tooltip, because `wiring` is free and throttled, `cheap` is a fifth of a cent, and
`quality` is the one that costs real money.

### 6.5 Pipeline page

The two-link form: club, WhoScored URL, SofaScore URL, season, competition,
matchweek, optional colours. Validate the club before submitting — it is validated
server-side before scraping for the same reason.

Then the live job: the seven stages as a vertical stepper with per-stage state,
from `GET /pipeline/status/{job_id}`. On success, link straight to the new match
page. On failure, show the stage and the actual error — `_diagnose()` already
classifies bot walls, missing event data and timeouts. Surface its message
verbatim; it is more useful than anything you would write.

### 6.6 System page

Not decoration — this system has real states worth showing:

- Service health: orchestrator, Postgres, MLflow, Airflow.
- DAG table: 39 fixture DAGs, next run, last result. **The 3 agent DAGs shown as
  paused, with the reason**, until §2.1 lands.
- The seven readiness gates from `db/readiness.py`.
- Eval history from MLflow: score, factual accuracy, hallucination rate, cost per
  run — with the **floor at 0.50 drawn on the chart** against a measured 0.517, so
  the three percent of headroom is visible rather than discovered at 06:00 on a
  Monday.

---

## 7. Build order

Each phase ends with something usable. Do not start the next until the previous
one runs.

| Phase | Deliverable | Done when |
|---|---|---|
| **0** | `analyst` service skeleton, `/health`, Compose entry | it answers one non-streamed question |
| **1** | Design tokens, three-state theming, fonts, layout shell, club context | a static page renders correctly in light, dark and system |
| **2** | Season overview + match list | real data from the four views |
| **3** | **Match page + panel tabs** | the six mirrored rows read correctly on desktop and stacked on mobile |
| **4** | **Chat: streaming, reasoning strip, evidence drawer, verification** | all seven SSE events render; a refusal looks like an answer |
| **5** | Conversation persistence, history, titles | reopening shows evidence, not just prose |
| **6** | Pipeline page | a match run from two links, watched end to end |
| **7** | System page | readiness gates and the eval floor visible |
| **8** | Point the 3 Airflow DAGs at the analyst service over HTTP | STATUS.md open item 9 closes |

Phase 3 and Phase 4 are the product. If time runs out, ship those two.

---

## 8. Do not build

- **Free-form SQL input.** The allowlist is the anti-hallucination guarantee.
  A "run your own query" box removes it.
- **Editable stats.** The warehouse is derived; a UI write path makes it a second
  source of truth.
- **A second charting layer over the panels.** They are rendered by
  `visualizer/match_report.py` from the same data, with team colours already
  resolved. Add interactive charts only where a PNG genuinely cannot serve — the
  shot map and the timeline are the two real candidates.
- **An optimizer trigger button.** `agent/optimize.py` spends money and runs for a
  long time. Keep it a manual, deliberate command.
- **Fake confidence.** No percentage confidence scores, no "AI is thinking…"
  theatre, no invented certainty. The system's honesty is its differentiator; the
  interface must not undercut it.

---

## 9. Acceptance criteria

1. A cold visitor understands within one screen what the system knows and what it
   cannot answer.
2. A match page loads to first meaningful paint in under 2s with 19 PNGs on it.
3. The panel mirror is legible at 1440px, 1024px and 375px.
4. A chat answer streams its first stage indicator in under 2s, whatever the total
   latency.
5. `unsupported_numbers` is visible without expanding anything.
6. A refusal is indistinguishable from a successful answer in visual weight.
7. Every figure on screen is traceable to a query id or a document within two
   clicks.
8. Light, dark and system themes are all correct — check the un-stamped state
   explicitly; it is the one that breaks.
9. Full keyboard operation; visible focus throughout.
10. Switching club re-scopes every page without a reload.

---

## 10. References

| For | Read |
|---|---|
| Services, data flow, stage contracts | `docs/ARCHITECTURE.md` |
| Tables, views, the ERD | `docs/DATABASE.md`, `docs/ERD.md` |
| The agent: modules, tools, evaluation, tiers | `docs/AGENT.md` |
| Per-club schemas, the two-link run | `docs/MULTI_CLUB.md` |
| Current state, open items, what is blocked | `STATUS.md` |
| Every make target | `COMMANDS.md` |
| Visual overview of the whole system | `docs/OVERVIEW.html` |

---

Act as lead architect and designer. Challenge anything in this brief that the code
contradicts — the code is the authority, and this document was written against a
specific commit. Optimise for an instrument a football analyst would use every
matchday, not a demo that looks good once.
