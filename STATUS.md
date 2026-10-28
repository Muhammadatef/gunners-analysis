# System status

**As of 2026-08-08.** Written against the running stack and the live warehouse,
not inferred from the code. Every number below was queried, not remembered.

This is the single current-state document. It does not replace
[OPEN_ISSUES.md](OPEN_ISSUES.md) (a closed investigation, kept as a record) or
[docs/MAINTENANCE.md](docs/MAINTENANCE.md) (the production-readiness audit).
If those disagree with this file, this file is newer.

---

## In one paragraph

The pipeline works end to end and has proved it on real data: the whole of
Arsenal's 2025/26 Premier League season — 38 matches — is scraped, enriched,
rendered and loaded, with every score independently verified. On top of it now
sits an **analyst layer**: four derived tactical tables, a 4,349-document
semantic corpus in pgvector, and a DSPy agent that answers with cited evidence
and is architecturally unable to invent a statistic. All seven readiness gates
are green. The 2026/27 season is armed and waiting for its first fixture on
**21 August 2026**. All of it is **merged into `main`** as of 2026-08-08. The
analyst runs from the host venv only: its three Airflow DAGs cannot run,
because the agent needs SQLAlchemy 2 and Airflow 2.10 forbids it — open item 9,
and the one thing still outstanding.

---

## Data

### 2025/26 — complete

| | |
|---|---|
| Matches | **38 / 38** |
| Events | 55,130 |
| Shots | 868 |
| Player-match rows | 1,520 |
| Pass links | 8,464 |
| Players / teams | 502 / 20 |
| Fixtures | 38, all `ingested` |
| Rendered | 38 dashboards, 722 panels, 338 MB in `output/2025-2026/` |

Verified, not assumed:

- **Every score matches SofaScore's own final score.** The calendar carries each
  match's real result from a source the pipeline never touches; all 38 agree, so
  no fixture loaded with its home and away sides transposed.
- **Every match has xG and xGOT from SofaScore.** None fell back to a
  placeholder. `matches.xg_source` reads `sofascore` on all 38.
- Arsenal's season reconstructs correctly from the warehouse: P38 W26 D7 L5,
  71 scored, 27 conceded, 85 points, 65.84 xG.

### The analyst layer — built, measured

Derived from the warehouse, all regenerable from `events`:

| Table | Rows | What it is |
|---|---|---|
| `possession_sequences` | 11,182 | possessions, segmented on real turnovers |
| `pressure_events` | 11,585 | defensive engagements + counterpresses |
| `carries` | 12,026 | the metres driven between two touches |
| `match_tactical_features` | 76 | PPDA, directness, field tilt, per team |
| `semantic_documents` | 4,349 | prose + 384-dim embeddings, in pgvector |

Arsenal's season reads correctly against the cookbook's scales: **PPDA 10.76**
(elite pressing is 7–9, passive 15+), **field tilt 63.2%**, 481 possessions
ending in a shot, and Rice the leading carrier at 186 progressive carries.

**Agent evaluation.** Total OpenRouter spend for everything below: **$0.18.**

| Run | n | ran | score | factual | tool | halluc | cost |
|---|---|---|---|---|---|---|---|
| dev, free tier | 18 | 18 | 0.891 | 0.889 | 0.944 | 0.148 | $0.00 |
| **test (held out), cheap** | 38 | 38 | **0.517** | 0.342 | 0.526 | 0.105 | $0.002 |
| test (held out), Sonnet | 38 | **16** | 0.880 | 0.875 | **1.000** | 0.206 | $0.061 |

Two things this says, and both are the eval doing its job:

**Generalisation is the real gap.** 0.891 on dev against 0.517 on held-out
families, same model. The families the agent never trained on — `player_assists`,
`player_prog_passes` — are where it drops.

**That gap is mostly model capability, not design.** On the 16 held-out
questions Sonnet completed, tool selection was perfect (1.000) and the score was
0.880. The small model routes those same questions wrongly.

Sonnet stopped after 16 questions on a billing ceiling, not an error in the
agent — see the OpenRouter note under open items. The 0.370 figure MLflow holds
for that run counts the 22 that never reached a model as wrong answers; 0.880 on
the 16 that ran is the honest number, and it is stated with its n.

Baselines were recorded before any optimization, so Phase 2's gain is
falsifiable — and it needed to be: **BootstrapFewShot made it slightly worse**
(0.9213 → 0.9069 on dev) and was correctly not promoted.

**MIPROv2 did not complete.** Its first attempt died on a missing `optuna`
after fifteen minutes of bootstrapping (now checked in the first second, and
pinned in `agent/requirements.txt`); the second ran out of OpenRouter credit
mid-search. The command is `make optimize OPTIMIZER=mipro BUDGET=3` once credit
is added — it is the one piece of the plan that did not land.

### 2026/27 — armed, nothing played

| | |
|---|---|
| Matches | 0 (season starts 21 Aug 2026) |
| Fixtures | 40 rows for 38 matchweeks — **see open item 2** |
| DAGs | 39 registered (38 fixture + 1 maintenance), **all unpaused** |
| First fixture | gw01, Arsenal v Coventry, Fri 21 Aug 2026, 20:00 |

---

## Code

**Merged into `main` on 2026-08-08.** `worktree-season-backfill-2025-26` was 20
commits ahead and `main` had nothing of its own, so it went in as a
fast-forward. **530 unit tests across 22 files, all passing** (was 180).

New in this work:

| File | What it does |
|---|---|
| `airflow/fixtures/build_season_calendar.py` | derives a finished season's calendar from SofaScore |
| `orchestrator/backfill_season.py` | ingests it, match by match, resumably |
| `visualizer/palette.py` | club colours, and forcing the two teams apart |
| `db/readiness.py` | is there enough history in the warehouse to analyse? |
| `docs/BACKFILL.md` | how the whole-season path works |

---

## What running it for real exposed

Six bugs, none of which unit tests could have found, because each needed live
data from two providers to surface. All six were also on the **live 2026/27
path** — the fixture DAGs run the same stages.

**1. xG silently absent from 8 of 38 matches.** The check deciding whether two
feeds describe the same match compared club names by token overlap. WhoScored
abbreviates (`Man Utd`, `Spurs`, `Wolves`) where SofaScore spells out, so the
names shared nothing and enrichment refused to write. The DAGs are *more*
exposed than the backfill here: they call `enrich_xg` with no URL at all, so
they depend entirely on `find_event` matching both names.

**2. The same check matched Manchester United to Manchester City.** `united` and
`city` were stripped as noise, so both clubs reduced to `{manchester}`. Home and
away are positional throughout the pipeline, so that false positive would write
one club's xG onto another club's match with no warning.

**3. A real `0.0` xG stored as NULL.** `db/aggregate.py` used `M.hxg or None`,
which cannot tell a genuine zero from the 0.0 default. A side with no shots on
target has exactly 0.0 xGOT; three of the first ten matches did.

**4. Five club crests were WebP files named `.png`.** `matplotlib.imread` picks
its decoder from the extension, so Chelsea v Arsenal failed at render *after*
scrape, transform, enrichment and the quality gate had all passed. **Coventry
and Hull were two of the five** — and MW1 on 21 August is Arsenal v Coventry.

**5. Most crests were never found at all.** Filenames were derived from the team
name, so WhoScored's `Bournemouth` asked for `bournemouth.png` while the asset
was `afc_bournemouth.png`. Only Arsenal and Crystal Palace were hardcoded.
`logos.json` had the correct mapping and nothing read it.

**6. Both teams drawn in the same colour.** Arsenal are `#EF0107`; six clubs
wear a red that is effectively the same mark. Arsenal play each twice, so 12 of
38 reports drew both passing networks and a shared shot map in one indivisible
red. Bournemouth was worse — the colour map missed its name entirely and fell
back to a default that *was* Arsenal red, so the two teams were the same hex.

Plus one operational fault: **the xT grid was re-downloaded from GitHub once per
match**, and a DNS blip failed a match outright. Now cached after first fetch —
which matters most for a DAG firing at 23:00 on a Saturday with nobody watching.

---

## Open items

### 1. ~~Merge the branch before 21 August~~ — done 2026-08-08

Bugs 1–6 above are now on `main`, so the first live fixture no longer dies at
render on Coventry's crest. Two things surfaced on the way in, both worth
keeping because both would have been silent.

**The branch tracked two symlinks pointing back into the main checkout.** A
worktree gets its own working directory, so `data/` and `output/` start empty
inside it and the whole 2025/26 corpus is invisible; the fix at the time was a
symlink, and both got committed. Merged into the checkout they point at, they
become symlinks to themselves — and git does not treat that as a no-op, it
replaces the real directory with the link. That is 357 MB of raw match HTML,
processed JSON, eval runs and the xT grid, none of it reconstructible without
re-scraping. Untracked and gitignored now; the two `_latest` symlinks stay
tracked because they are relative and the pipeline writes them on purpose.

**The three agent DAGs have no runtime**, and cannot simply be given one. That
turned out to be its own problem — see open item 9. No rebuild is needed for
this merge; the pipeline code is bind-mounted.

### 2. Two duplicate fixture rows in 2026/27

Both Newcastle fixtures exist twice, under two slugs:

```
gw11_2026-11-21_newcastle-united_v_arsenal   +  gw11_2026-11-21_newcastle_v_arsenal
gw22_2027-01-23_arsenal_v_newcastle-united   +  gw22_2027-01-23_arsenal_v_newcastle
```

`fixture_key` is the upsert conflict key, so when the calendar's Newcastle slug
changed, seeding inserted new rows instead of updating. The DAGs are generated
from the JSON (38 of them), so there are no duplicate DAGs — but two rows will
sit permanently `scheduled` and `v_season_progress` reports 40. Delete the two
stale `newcastle-united` rows; neither is referenced by a match.

### 3. ~~`events.qualifiers` is not queryable~~ — fixed 2026-08-08

All 55,130 rows held stringified Python reprs, so the GIN index was dead and no
qualifier could be filtered in SQL. Parsed at the boundary between the render
path and the warehouse (`db/aggregate.parse_qualifiers`) and backfilled by
`db/migrations/001_qualifiers_to_jsonb.py`. All 55,130 converted, 0 unparseable,
and the index is genuinely used — a containment query for crosses returns 1,180
rows through it.

### 4. Still unproven: the DAG wrapper itself

The *stages* now have 38 real runs behind them. What has never executed is the
layer around them — the one-shot four-day schedule window, `resolve_match_id`
against a fixture whose id is not published yet, the six-retry backoff, and the
maintenance DAG's backfill. MW1 on 21 August is the first live test. Watch it.

### 5. From the production audit, unchanged

- **No alerting.** A DAG failing at 23:00 on a Saturday tells nobody.
  `pipeline_runs` and `fixtures.last_error` already hold what a notifier needs.
- **Default passwords.** `POSTGRES_PASSWORD=gunners`, `AIRFLOW_PASSWORD=admin`,
  and `.env` is committed alongside `.env.example`.
- **No backups.** A season of event data in one Docker volume, no dump schedule.
- **SofaScore-only matches cannot reach the warehouse** — the schema is
  event-grained and those matches have no events.
- **Airflow runs `standalone`**, which is not a production topology.

### 6. ~~Derived features for the analyst roadmap~~ — built 2026-08-08

All seven readiness gates are green and `db/readiness.py` now reports
**READY**. Carries, possession chains and pressing events are derived and
stored; gate 7 was rewritten from "ColBERTv2 index populated" to "Vector index
populated" because the spec moved to pgvector.

**Per-shot xG remains the one genuine gap.** `shots.xg` is NULL throughout —
match-level xG comes from SofaScore and no shot-level model exists. Questions of
the form "which chance was the best" are in the eval set as things the agent
must *refuse*, which is the honest handling until a model is built.

### 7. OpenRouter credit is exhausted — add credit to continue

**This is the one thing that needs your action for the agent to keep running.**

The API reports `limit: 15`, `remaining: 14.81`. That is the *key's spending
cap*, not money. The account is free tier, its actual allowance was about
**$0.19, and the night's work spent it** — `usage: $0.1938`. Every model now
returns:

```
402: This request requires more credits, or fewer max_tokens.
```

Paid calls stopped mid-way through the MIPROv2 run. Nothing is broken; there is
simply no credit. Add some at <https://openrouter.ai/settings/credits> and both
the cheap and quality tiers work immediately. The **free tier
(`openai/gpt-oss-20b:free`) still works and costs nothing** — `make eval` and
`make ask` default to it.

A related trap, now fixed: DSPy sends no `max_tokens`, so LiteLLM requests the
model's ceiling (64,000 for Sonnet) and OpenRouter pre-authorises the *worst
case* rather than the real cost. Without a cap every Sonnet call is refused
however cheap the answer would be. `agent/config.py` caps completions at 1,024.

### 8. The agent's own limits

- Retrieval matches words, not magnitudes: "a long passing move" will not
  reliably return the longest. Numeric questions route to SQL for this reason.
- Only Arsenal's matches are in the warehouse, so cross-club player comparisons
  are refusals rather than answers.
- Phase 3 of the spec (six more modules, fifteen more tools) is deliberately
  unbuilt — the spec says add them when the eval shows a measured gap.
- **A transport failure is scored as a wrong answer.** A question whose call
  never reached a model counts against factual accuracy exactly like a
  hallucination. It is why the Sonnet run above reads 0.370 in MLflow and
  0.880 on the 16 that ran, and a dev re-run on 2026-08-08 scored 0.111 with
  15 of 18 questions rate-limited by the free tier before they got anywhere.
  Any run on `wiring` is a smoke test, never a regression check.
- **`agent_eval_regression` has 3% of headroom.** It fails below
  `GUNNERS_EVAL_FLOOR=0.5` and the held-out score on the cheap tier is 0.517,
  so an ordinary wobble — or a handful of rate-limited calls, per the point
  above — trips it. Combined with open item 5 it fails at 06:00 on a Monday
  and tells nobody. Raise the floor deliberately or widen the gap; do not
  leave it at a number that was never chosen against the measurement.

### 9. The agent cannot share an interpreter with Airflow — blocking the DAGs

The three agent DAGs (`agent_documents_refresh` nightly,
`agent_eval_regression` Mondays, `agent_optimize` manual) import `agent.*`
inside their task bodies, per the convention in `airflow/DAGS.md`. None of
dspy, optuna, pgvector, sqlalchemy, fastembed or mlflow is installed in any
image — `agent/requirements.txt` is referenced by no Dockerfile and no compose
file. Because the imports are inside the tasks, DAG parsing stays green and
nothing looks wrong until a task fires at 04:30 and dies on `ModuleNotFound`.

**The obvious fix does not work, and fails dangerously.** Adding the six to
`airflow/requirements.txt` was built and measured on 2026-08-08:

- `agent/models.py` needs **SQLAlchemy ≥ 2.0** — `DeclarativeBase`, `Mapped`,
  `mapped_column` are 2.0 APIs.
- **Airflow 2.10.4 pins SQLAlchemy < 2.0.**
- pip resolves that by installing 2.0.51, prints the conflict as a *warning*,
  and exits 0. The image builds successfully.
- The result imports `airflow` fine — and cannot load its own ORM:

  ```
  from airflow.models import TaskInstance
  -> MappedAnnotationError: Type annotation for "TaskInstance.dag_model"
     can't be correctly interpreted for Annotated Declarative Table form
  ```

That is the scheduler's data model. Deploying it takes down **all 39 fixture
DAGs**, five weeks before the season opens, in exchange for three agent DAGs.
A green build and a successful `import airflow` both say nothing here.

The two versions have to live in separate interpreters. The likely shape is a
second venv in the Airflow image plus `@task.external_python`, which is what
that operator exists for — but `external_python` task bodies do not close over
the DAG module's globals, so the three tasks need their constants passed as
arguments first. Not attempted; scoped, not built.

**Until then the analyst is host-only:** `LOCAL=1 make ask` and
`LOCAL=1 make eval` against the venv, which has no Airflow in it and so no
conflict. Freshness is manual — `make documents` after new matches land.

---

## Running it

```bash
# the analyst
make agent-setup                    # tactical tables, documents, eval set, gates
make eval SPLIT=dev                 # baseline, free tier, costs nothing
make ask Q="Was Arsenal a high-pressing side in 2025/26?"
make optimize OPTIMIZER=bootstrap BUDGET=2
# MLflow: http://localhost:5000

# stack
make up && make health

# a finished season
make calendar BACKFILL_SEASON='2025/2026'   # derive from SofaScore
make backfill-plan                          # what would run, touching nothing
make backfill                               # ~40 min for 38 matches, resumable
make readiness                              # coverage + gates

# one match, any club, from two links
make match CLUB=barcelona WS='<whoscored>' SS='<sofascore>'

# the season in the warehouse
make season && make table && make players
make test                                   # 530 unit tests
```

Full command reference: [COMMANDS.md](COMMANDS.md).
Backfill detail: [docs/BACKFILL.md](docs/BACKFILL.md).
Everything else: [INDEX.md](INDEX.md).
