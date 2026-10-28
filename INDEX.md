# Documentation index

## Start here

| Doc | What it covers |
|---|---|
| [STATUS.md](STATUS.md) | **Where the system is right now** — data loaded, what's proven, what's outstanding |
| [README.md](README.md) | What the project does, quick start, repo layout, limitations |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Services, data flow, warehouse model, design decisions, known issues |
| [docs/AUTOMATION.md](docs/AUTOMATION.md) | The 38 fixture DAGs, scheduling maths, maintenance, reschedules |
| [airflow/DAGS.md](airflow/DAGS.md) | DAG shapes — task graphs, XComs, the fixture status handshake |
| [docs/AGENT.md](docs/AGENT.md) | The Football Analyst AI Agent — why it cannot invent a statistic |
| [docs/BACKFILL.md](docs/BACKFILL.md) | Ingesting a season that has already been played, resumably |
| [docs/DATABASE.md](docs/DATABASE.md) | Schema reference, views, query recipes, re-ingest, backup |
| [docs/ERD.md](docs/ERD.md) | Entity relationship diagram, every foreign key and delete rule |
| [docs/VISUALS.md](docs/VISUALS.md) | What each of the 18 panels measures, and where the port diverges from the notebook |
| [COMMANDS.md](COMMANDS.md) | Every command, with its `make` shortcut |
| [docs/MULTI_CLUB.md](docs/MULTI_CLUB.md) | Per-club schemas, the two-link manual run, real-browser scraping |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Why WhoScored is primary, what SofaScore adds, what the alternatives can't do |
| [docs/MAINTENANCE.md](docs/MAINTENANCE.md) | Repository audit and production-readiness gaps |

## New to football data?

[**docs/FOOTBALL_DATA_COOKBOOK.md**](docs/FOOTBALL_DATA_COOKBOOK.md) — a
standalone reference for anyone entering the industry: the four levels of
football data, the event model, coordinate systems, the full event-type and
qualifier taxonomy, every KPI with its formula (xG, xGOT, xT, progressive
passes, field tilt, PPDA), spatial concepts, ten ways to fool yourself, and a
six-week learning path. Every definition is implemented in this repo and every
number comes from one real match.

## Legacy documents

`QUICKSTART.md`, `USAGE.md`, `DEPLOYMENT.md` and `TROUBLESHOOTING.md` predate
Airflow and the warehouse. Each now carries a staleness banner; they reference
scripts that were removed in the cleanup (`run.sh`, `test_arsenal_match.py`,
`visualizer/visualize.py`). Treat README.md, `COMMANDS.md` and `docs/` as
authoritative.

## Common tasks

```bash
make up                    # start postgres, airflow, orchestrator, redis
make schema && make seed   # apply the schema, load the 2026/27 calendar
make render                # render the newest scraped match into output/
make load JOB=<job_id>     # load it into the warehouse
make fixtures              # regenerate the calendar after a reschedule
make season                # what has run and what has not
make test                  # 217 unit tests

# a season that has already been played, all 38 matches
make calendar BACKFILL_SEASON='2025/2026'
make backfill              # resumable; re-run it to retry whatever failed

# any club, from two links (WhoScored for events, SofaScore for xG)
make schema-club CLUB=barcelona
make match CLUB=barcelona WS='<whoscored-url>' SS='<sofascore-url>' \
           COMP='Champions League' MW=6
```

`make help` lists everything. Full reference, including the raw commands behind
each target: [COMMANDS.md](COMMANDS.md).

## Removed in the cleanup

The twelve overlapping status files (`FINAL_STATUS.md`, `MISSION_COMPLETE.md`,
`SUCCESS.md`, `WAKE_UP.md`, `START_HERE.md`, `SUMMARY.md`, `SYSTEM_READY.md`,
`READ_ME_FIRST.txt`, `DASHBOARD_WITH_DATA_READY.txt`, `VIEW_DASHBOARD.txt`,
`FINAL_SUCCESS_STATUS.md`, `TODO.md`) and four superseded visualisers
(`visualize.py`, `visualize_old.py`, `visualize_v2.py`, `visualize_empty.py`)
were deleted, along with `test_arsenal_match.py` and `run.sh`, which posted a
request schema the API no longer accepts.

All of it remains in git history at the initial commit — `git show <commit>:<path>`
recovers any file.

[STATUS.md](STATUS.md) is the deliberate single replacement for those twelve. It
carries a date, states what was measured rather than what was hoped, and is the
only file allowed to describe current state — if a second one appears, one of
them is already wrong.
