# Command reference

Every command here has been run against the current codebase. Most of them have
a `make` shortcut — `make help` lists them all.

Python work runs inside the `airflow` container by default: it carries the
analytics stack, has the repo bind-mounted at `/opt/gunners`, and already knows
`GUNNERS_DB_URL`. Prefix a target with `LOCAL=1` to use your own venv instead.

### `LOCAL=1` and the warehouse DSN

Inside compose the DSN host is `postgres`. On the host that name does not
resolve (`could not translate host name "postgres"`). `make LOCAL=1 …` therefore
defaults `GUNNERS_DB_URL` to `postgresql://gunners:…@localhost:5432/gunners`
when you have not already exported one. Override if your password differs:

```bash
LOCAL=1 POSTGRES_PASSWORD=change-me make ask Q="How many points?"
# or:
GUNNERS_DB_URL='postgresql://gunners:change-me@localhost:5432/gunners' \
  LOCAL=1 make ask Q="How many points?"
```

The analyst (`ask` / `eval` / `optimize`) **must** use `LOCAL=1`: it needs
SQLAlchemy 2 and cannot share Airflow's interpreter. See [docs/AGENT.md](docs/AGENT.md).

---

## The stack

Five services: `postgres` (the warehouse, `:5432`), `airflow-db` (Airflow's own
metadata, deliberately separate), `redis`, `orchestrator` (`:8000`), `airflow`
(`:8080`).

```bash
cp .env.example .env       # set POSTGRES_PASSWORD and AIRFLOW_PASSWORD first

make up                    # docker compose up -d
make ps                    # service status
make logs                  # follow everything
make logs SVC=airflow      # follow one service
make health                # GET /health, pretty-printed
make ui                    # wait for Airflow to bind, then open it
make api                   # open the API docs
make down                  # stop; the warehouse volume survives
make restart               # down + up
make build                 # rebuild the orchestrator and airflow images
```

- Airflow UI — <http://localhost:8080> (`admin` / `AIRFLOW_PASSWORD`)
- API docs — <http://localhost:8000/docs>
- Warehouse — `postgresql://gunners@localhost:5432/gunners`

> After pulling changes that touch `orchestrator/` you need `make build &&
> make up`, not just `make up` — compose will happily keep running an image
> built from older code. If `curl localhost:8000/` reports anything other than
> `"service": "GunnersAnalysis"`, the container is stale.

> The Airflow webserver takes ~100s to bind and `standalone` kills it if
> gunicorn has not answered in 120s, so it sometimes never comes up while the
> container still reports `Up`. `make ui` waits for a real `200` instead of
> guessing. If it times out: `docker compose logs airflow | grep -i webserver`.

## The warehouse

```bash
make schema      # apply db/schema.sql - idempotent, safe to re-run
make seed        # seed the fixture calendar from airflow/fixtures/*.json
make fixtures    # rebuild the calendar JSON, then seed it
make psql        # psql shell with search_path already set to gunners
```

Behind the make targets:

```bash
docker compose exec -T airflow python /opt/gunners/db/connection.py --schema gunners
docker compose exec -T airflow python /opt/gunners/db/seed_fixtures.py [--file <json>]
docker compose exec -T airflow python /opt/gunners/airflow/fixtures/build_fixtures.py
docker compose exec postgres psql -U gunners -d gunners
```

### One club, one schema

`gunners` is Arsenal's. Any other club gets its own complete clone of the same
14 tables and 4 views, so nothing of Barcelona's is ever mixed into Arsenal's
numbers:

```bash
make schema-club CLUB=barcelona     # -> schema `barcelona`
make schema-club CLUB='AC Milan'    # -> schema `ac_milan`
```

`make match` (below) provisions the schema itself, so this is only needed when
you want the tables ahead of the first run. Every query target takes `SCHEMA`:

```bash
make table SCHEMA=barcelona
make psql  SCHEMA=ac_milan
```

Club names are slugified and validated before they reach SQL - lowercase ascii,
no reserved names. `make season` is Arsenal-only in practice: only Arsenal has a
seeded fixture calendar, so `v_season_progress` is empty in other schemas. The
match, event, shot and player tables are fully populated regardless.

Ready-made season queries, all reading the views in
[docs/DATABASE.md](docs/DATABASE.md):

```bash
make season      # v_season_progress  - calendar vs reality, kick-off order
make table       # v_team_season_summary - league table
make players     # v_player_season_totals - per-90s, best xT first
```

Schema shape and every foreign key: [docs/ERD.md](docs/ERD.md).

## One match by hand

The scheduled path is Airflow. This is the manual equivalent, useful for a
friendly, a cup tie, or a re-render.

```bash
make render                       # newest scraped job -> output/
make render JOB=<job_id>          # a specific job
make load JOB=<job_id>            # into Postgres
make report JOB=<job_id>          # render then load
```

`JOB` is the id of a directory under `data/processed/`. `make render` without
one picks the most recent scrape.

Full flags:

```bash
python visualizer/match_report.py \
  --job <job_id> \            # default: newest scrape
  --out output \              # flat output directory
  --output-root output \      # or the season layout
  --season 2026/2027 \
  --matchweek 17 \
  --dpi 150 \
  --only panels|dashboard     # skip the other half

python db/loader.py \
  --job <job_id> \            # required
  --season 2026/2027 \
  --fixture-key gw17_2026-12-26_crystal-palace_v_arsenal \
  --out-dir output/2026-2027/mw17_... \
  --dag-run-id <airflow run id>
```

Renders land in `output/<season>/<matchweek>_<date>_<home>_v_<away>/`, with a
`match.json` manifest per match and a rebuilt `_index.json` per season.

## Any club, from two links

The full pipeline in one command, for any club in any competition. It takes
**two** URLs for the same match, because neither source is complete on its own:
WhoScored carries the Opta event stream the 18 panels are built from but no xG
at all, and SofaScore carries xG and xGOT but no pass-by-pass feed.

```bash
make match CLUB=barcelona \
  WS='https://www.whoscored.com/matches/1234567/live/...' \
  SS='https://www.sofascore.com/football/match/barcelona-chelsea/xyz' \
  COMP='Champions League' MW=6
```

Behind it:

```bash
python orchestrator/run_match.py \
  --club barcelona \             # -> schema `barcelona`, output/barcelona/
  --whoscored '<url>' \          # the event stream
  --sofascore '<url>' \          # xG and xGOT; required unless --no-xg
  --season '2025/2026' \
  --competition 'Champions League' \
  --matchweek 6 \
  --min-events 500 \             # relax the PL-calibrated quality gate
  --home-color '#A50044' --away-color '#FEBE10' \
  --mode headed \                # headed | xvfb | headless
  --no-load                      # render only, skip the warehouse
```

It runs synchronously and exits non-zero on failure. Stages in order: scrape →
transform → **enrich from SofaScore** → validate → render → load.

A few things worth knowing:

* **The two links must be the same match.** Before writing any xG the pipeline
  compares SofaScore's home/away against WhoScored's. Reversed sides are
  swapped automatically (`xg_source: sofascore (sides swapped)`); a genuinely
  different match writes nothing and says so. Home/away identity is positional
  throughout this codebase, so this check is the only thing standing between a
  mispasted link and confident, wrong xG in `matches.home_xg`.
* **`--competition` matters off the Arsenal path.** WhoScored's payload never
  names the competition, and the default is `Premier League` - which would file
  a Champions League tie under the wrong name in both the database and the
  output directory.
* **`--home-color` / `--away-color` too.** The curated colour map only covers
  the Premier League, so a Barcelona report without them renders in placeholder
  colours.
* **`--min-events`** relaxes the quality gate, which is calibrated to a Premier
  League match (~1200-2000 Opta events). A sparser feed trips the 800 floor on a
  perfectly complete match.
* If WhoScored has no event stream for the match at all - common for friendlies
  and lower-tier competitions - the run falls back to SofaScore and renders the
  reduced nine-panel report rather than failing.

The same thing over HTTP, if you want it fire-and-forget:

```bash
curl -X POST http://localhost:8000/pipeline/run \
  -H 'Content-Type: application/json' \
  -d '{"match_url": "<whoscored>", "sofascore_url": "<sofascore>",
       "club": "barcelona", "season": "2025/2026",
       "competition": "Champions League"}'
```

Output for a club lands one directory deeper, so each club keeps its own index
and `_latest`:

```
output/barcelona/2025-2026/mw06_2025-11-26_barcelona_v_chelsea/
```

Arsenal is the exception: `--club gunners` still writes to `output/<season>/`,
because that is where the DAGs have always put it.

## Scraping

The scraper runs a **real browser** by default. Headless Chrome advertises
itself in a dozen small ways - a `HeadlessChrome` user-agent token,
`navigator.webdriver`, no GPU, no input events ever - and WhoScored's front door
notices.

```bash
python scraper/scraper.py '<whoscored-url>' \
  --output data/raw/match.html \
  --wait 15 \                   # seconds to poll for the match payload
  --mode headed \               # headed | xvfb | headless
  --attempts 2                   # retry with a fresh browser
```

Mode resolution, highest precedence first: `--mode`, then
`GUNNERS_BROWSER_MODE`, then auto - a real window if `$DISPLAY` is set, else
Xvfb, else headless. Containers have no X server, so compose pins them to
`xvfb`: still real Chrome with a real renderer, only the display is virtual.
**For an actually visible window, run on the host with `LOCAL=1`.**

The browser keeps a persistent profile at `data/browser-profile/`, so cookies
and any consent clearance survive between runs. Delete it to start clean.

If a page loads but carries no `matchCentreData`, the error says which of the
three cases it is: a bot wall (retry), a truncated download, or a match
WhoScored published without event data (retrying will not help - use the
SofaScore fallback).

## A finished season, all at once

The DAGs cover a season as it happens. A season already played is a backfill:
derive its calendar, then ingest it match by match. Full detail in
[docs/BACKFILL.md](docs/BACKFILL.md).

```bash
# 1. derive the calendar from SofaScore - 38 fixtures, with each match's
#    sofascore event id and its real final score
python airflow/fixtures/build_season_calendar.py --season 2025/2026
make calendar BACKFILL_SEASON='2025/2026'          # same thing

# 2. resolve the WhoScored match ids - one page load for the whole season
python orchestrator/backfill_season.py --season 2025/2026 --resolve-ids

# 3. see what would run, touching nothing
python orchestrator/backfill_season.py --season 2025/2026 --dry-run
make backfill-plan

# 4. run it (~1 hour for 38 matches; xvfb keeps Chrome off your desktop)
python orchestrator/backfill_season.py --season 2025/2026 --mode xvfb
make backfill

# just a few matchweeks, or re-ingest ones already loaded
make backfill ONLY='13,20-22'
make backfill ONLY=38 FORCE=1
```

Resume is a query against `matches.match_id`, so re-running the same command
retries only what is missing. Each match is checked against the calendar's
final score and for xG *and* xGOT; anything short is reported rather than
counted as done. Progress lands in `data/backfill/<season>.json`.

```bash
# how complete is the warehouse?
python db/readiness.py --season '2025/2026'
make readiness
```

## Season automation

38 per-fixture DAGs plus one maintenance DAG, all generated from
`airflow/fixtures/arsenal_2026_27.json`.

```bash
# what exists, and whether it will actually fire
docker compose exec airflow airflow dags list
docker compose exec airflow airflow dags list-import-errors

# DAGs are created paused. Nothing runs until they are turned on:
docker compose exec airflow airflow dags unpause arsenal_season_maintenance
for d in $(docker compose exec -T airflow airflow dags list 2>/dev/null \
           | awk -F'|' '/^arsenal_2026_2027/ {gsub(/ /,"",$1); print $1}'); do
  docker compose exec -T airflow airflow dags unpause "$d"
done

# one fixture, on demand
docker compose exec airflow airflow dags trigger \
  arsenal_2026_2027_gw01_2026-08-21_arsenal_v_coventry

# force a match id when WhoScored has not published one yet
docker compose exec airflow airflow variables set \
  match_id_gw01_2026-08-21_arsenal_v_coventry 1903353

docker compose exec airflow airflow tasks states-for-dag-run <dag_id> <run_id>
```

Each fixture DAG is scheduled by a cron pinned to 30 minutes after its own final
whistle (`POST_MATCH_DELAY_MINUTES` after kick-off + `MATCH_DURATION_MINUTES`,
i.e. kick-off + 2h25 by default), bracketed by `start_date` / `end_date` so it
fires once and only once. `arsenal_season_maintenance` runs Mondays 06:00 UK:
re-seeds the calendar, resolves match ids for the next 21 days, and backfills
anything that missed its slot. Detail: [docs/AUTOMATION.md](docs/AUTOMATION.md).

## The API

FastAPI on `:8000`, for ad-hoc runs and warehouse reads. Scheduled fixtures are
Airflow's job, not this service's.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/` | service identity and configured season |
| `GET` | `/health` | redis and postgres reachability |
| `POST` | `/pipeline/run` | run the full pipeline for one match URL |
| `GET` | `/pipeline/status/{job_id}` | pending / running / completed / failed |
| `GET` | `/visualizations/{job_id}` | rendered panel list |
| `GET` | `/season/progress` | `v_season_progress` |
| `GET` | `/season/table` | `v_team_season_summary` |
| `GET` | `/season/players` | `v_player_season_totals` |
| `GET` | `/matches/{match_id}` | one match with both teams' stats |

Every read endpoint takes `?club=` to query another club's schema; without it
they read `gunners`.

```bash
curl -X POST http://localhost:8000/pipeline/run \
  -H 'Content-Type: application/json' \
  -d '{
    "match_url": "https://www.whoscored.com/matches/1903353/live/england-premier-league-2026-2027-crystal-palace-arsenal",
    "sofascore_url": "https://www.sofascore.com/football/match/crystal-palace-arsenal/xyz",
    "season": "2026/2027",
    "fixture_key": "gw17_2026-12-26_crystal-palace_v_arsenal",
    "load_to_warehouse": true
  }'

curl -s http://localhost:8000/pipeline/status/<job_id> | jq
curl -s 'http://localhost:8000/season/table?club=barcelona' | jq
```

`match_url` is the only required field. Supply `sofascore_url` and xG/xGOT are
filled in from it; supply `home_xg`/`away_xg`/`home_xgot`/`away_xgot` to set
them by hand; supply neither and the shot map and goalkeeper panels render
`0.0` placeholders — they are **not** in the WhoScored feed.

`club` routes the run into that club's schema and output directory, and creates
the schema on first use. `competition`, `matchweek` and `min_events` behave as
they do on the CLI.

Both callers execute the same `pipeline.run_match`, so the SofaScore fallback
and the sides check apply here too. `/visualizations/{job_id}` finds a job's
PNGs in either layout — the season directory first, the flat
`data/visualizations/<job>` second — and returns the `match_dir` they live in.

## Individual stages

Each stage is a module you can drive on its own, which is what the DAG tasks do
internally.

```bash
# scrape
python scraper/scraper.py \
  "https://www.whoscored.com/matches/1903353/live/..." \
  --output data/raw/match.html --wait 15

# transform -> EventData.csv, PlayerData.csv, metadata.json
python transformer/transform.py data/raw/match.html \
  --output-dir data/processed/<job_id>

# validate
python quality/validate.py --job <job_id> --output data/quality/<job_id>.json
python quality/validate.py \
  --events data/processed/<job_id>/EventData.csv \
  --players data/processed/<job_id>/PlayerData.csv \
  --output data/quality/report.json
```

## The analyst (DSPy)

Host-only — always `LOCAL=1`. The Makefile rewrites `GUNNERS_DB_URL` to
`localhost` for those runs (see above). Detail: [docs/AGENT.md](docs/AGENT.md).

```bash
make readiness BACKFILL_SEASON='2025/2026'   # seven gates; expect READY

# Ask (use fallback while free-tier daily quota is exhausted)
LOCAL=1 make ask TIER=fallback Q="Was Arsenal a high-pressing side in 2025/26?"
LOCAL=1 make ask TIER=fallback Q="What is Arsenal's transfer budget next summer?"
venv/bin/python -m agent.ask --tier fallback --show-evidence \
  "Who had the most progressive carries for Arsenal in 2025/26?"

# Eval smoke + MLflow (http://localhost:5000, experiment football-analyst)
LOCAL=1 make eval SPLIT=dev TIER=fallback LIMIT=3

# Optimize — needs OpenRouter credits; free tier alone is not enough
LOCAL=1 make optimize OPTIMIZER=mipro BUDGET=3 TIER=cheap
# or a lighter compile while iterating:
LOCAL=1 make optimize OPTIMIZER=bootstrap BUDGET=1 TIER=fallback TRAIN_LIMIT=4
```

Tiers: `wiring` (free, 50 req/day), `cheap`, `quality`, `fallback` (`gpt-4o-mini`).
When `wiring` returns 429 `free-models-per-day`, use `TIER=fallback` or add
credits at https://openrouter.ai/settings/credits.

## Tests

```bash
make test        # unit tests, quiet
make test-cov    # with coverage -> htmlcov/index.html
```

`make test` prefers `./venv/bin/python` when it exists and falls back to
`python3`. Coverage needs `pytest-cov`, which is in the venv but not in a bare
system Python — that is why the coverage flags live in the `test-cov` target
rather than in `pytest.ini`.

```bash
venv/bin/python -m pytest tests/unit/test_transformer.py -v
venv/bin/python -m pytest tests/unit/test_season_schedule.py::test_slug_matches_the_two_teams -v
venv/bin/python -m pytest tests/unit -k xt -v        # 6 tests
```

## Data on disk

```
data/raw/            scraped match-centre HTML          (gitignored)
data/processed/      <job_id>/EventData.csv, PlayerData.csv, metadata.json
data/quality/        validation reports                 (gitignored)
output/<season>/     rendered panels + match.json + _index.json
logs/                                                   (gitignored)
```

```bash
ls -t data/processed | head           # most recent job ids
jq . data/quality/*.json | head -40
jq '.matches[] | {dir, score}' output/2026-2027/_index.json
```

```bash
make clean                 # wipe data/raw, processed, quality, visualizations
make reset CONFIRM=1       # also drop the Postgres volume - every match goes
```

`make clean` only removes regenerable files. `make reset` destroys the
warehouse, so it refuses to run without `CONFIRM=1`.

## Debugging

```bash
make health
docker compose ps
docker compose logs --tail=100 orchestrator
docker compose logs -f airflow | grep -iE 'error|traceback'

# is the warehouse actually populated?
make psql
  \dt
  SELECT count(*) FROM events;
  SELECT * FROM v_season_progress WHERE status <> 'scheduled';

# what did the last pipeline run do?
docker compose exec -T postgres psql -U gunners -d gunners -c \
  "SELECT job_id, stage, status, duration_s, error FROM gunners.pipeline_runs
   ORDER BY started_at DESC LIMIT 10;"

# redis job state (ephemeral, 24h TTL)
docker compose exec redis redis-cli KEYS 'job:*'
docker compose exec redis redis-cli GET job:<job_id>
```

Common causes, in the order they usually bite:

| Symptom | Cause |
|---|---|
| DAGs exist but never run | they are created paused — unpause them |
| `resolve_match_id` skips | WhoScored has not published the id yet; the maintenance DAG retries, or set the `match_id_<fixture_key>` Variable |
| Panels render with `0.0` xG | xG is not in the feed; pass it on the request or in `metadata.json` |
| API reports version `1.0.0` | stale orchestrator image — `make build && make up` |
| `pytest` "unrecognized arguments" | running system Python without `pytest-cov`; use `make test` |

## Related documents

| Doc | Covers |
|---|---|
| [README.md](README.md) | what the project does, quick start, layout |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | services, data flow, design decisions |
| [docs/AUTOMATION.md](docs/AUTOMATION.md) | the 38 DAGs, scheduling maths, reschedules |
| [docs/DATABASE.md](docs/DATABASE.md) | schema reference, views, query recipes |
| [docs/ERD.md](docs/ERD.md) | the diagram, every foreign key, delete rules |
| [docs/VISUALS.md](docs/VISUALS.md) | what each of the 18 panels measures |
