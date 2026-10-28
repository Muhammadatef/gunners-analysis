# Open issues

Written 2026-08-05 while adding `docs/ERD.md` and rewriting `COMMANDS.md` and
the `Makefile`. Everything below was verified against the running stack, not
inferred from the code.

All four are now closed. Resolved 2026-08-05; details under each item.

---

## 1. Nothing will run when Arsenal play — all 39 DAGs are paused

**Status: FIXED 2026-08-05** — all 39 unpaused. Verified zero fixture windows had
opened at the time, so nothing fired retroactively; the first opens 21 Aug.
Note the loop below does not work as written: `docker compose exec` inside a
host-side `for` loop silently no-ops. Run the loop *inside* the container.



```
$ docker compose exec airflow airflow dags list
paused: 39   unpaused: 0
```

The DAGs are registered and import cleanly (`airflow dags list-import-errors`
is empty). They simply have never been switched on, and a paused DAG never
fires.

This does not fix itself by waiting. Each fixture DAG is bracketed:

```python
schedule   = cron pinned to 30 min after the final whistle   # kickoff + 2h25
start_date = run_at.subtract(days=1)
end_date   = run_at.add(days=3)
catchup    = False
```

That is a **one-shot four-day window**. If a DAG is paused while its window
passes, it can never fire for that fixture. The safety net is
`arsenal_season_maintenance` (Mondays 06:00 UK), which re-seeds the calendar,
resolves WhoScored match ids for the next 21 days, and backfills fixtures that
missed their slot — and that one is paused too.

First fixture is **gw01, Arsenal v Coventry, Fri 21 Aug 2026, 20:00**, so its
DAG fires at 00:00 on the 22nd. For the maintenance DAG to resolve its match id
inside the 21-day lookahead it has to be running by **Monday 10 August**.

### Fix

```bash
docker compose exec airflow airflow dags unpause arsenal_season_maintenance

for d in $(docker compose exec -T airflow airflow dags list \
           | awk -F'|' '/^arsenal_2026_2027/ {gsub(/ /,"",$1); print $1}'); do
  docker compose exec -T airflow airflow dags unpause "$d"
done

# confirm
docker compose exec airflow airflow dags list | grep -c 'False'   # want 39
```

Then verify nothing else is in the way:

```bash
make health          # postgres + redis reachable
make season          # 38 fixtures, all 'scheduled'
```

You cannot do any of this through the web UI until issue 4 is understood — the
webserver had been dead for a day when this was written.

### Worth deciding at the same time

Unpausing starts live scraping of WhoScored on a schedule. Consider a dry run
first — trigger one DAG by hand and watch it end to end:

```bash
docker compose exec airflow airflow dags trigger \
  arsenal_2026_2027_gw01_2026-08-21_arsenal_v_coventry
docker compose exec airflow airflow tasks states-for-dag-run <dag_id> <run_id>
```

`resolve_match_id` will raise `AirflowSkipException` until WhoScored publishes
the id. To force it:

```bash
docker compose exec airflow airflow variables set \
  match_id_gw01_2026-08-21_arsenal_v_coventry <whoscored_match_id>
```

---

## 2. The running orchestrator container is a stale build

**Status: FIXED 2026-08-05** — rebuilt and verified: `/` reports
`"service":"GunnersAnalysis"`, `/health` includes `"postgres":"connected"`, and
`/season/table` returns rows. Two earlier build attempts were killed by a
timeout before the image-export step, which is why the old image lingered.



```bash
$ curl -s localhost:8000/
{"service":"Football Analytics Pipeline","status":"running","version":"1.0.0"}
```

`orchestrator/api.py` in the repo is **v2.0.0**, titled `GunnersAnalysis`, with
a Postgres check on `/health` and the `/season/*` and `/matches/{id}` endpoints.
The container is running an image built before that rewrite — note it was
started as `football-orchestrator` with the command `uvicorn api:app`, while
`docker-compose.yml` now says `gunners-orchestrator` and
`uvicorn orchestrator.api:app`. `docker compose up -d` alone will not replace
it; the image has to be rebuilt.

Consequences today: `/health` reports `healthy` without ever checking Postgres,
and the three season endpoints 404.

### Fix

```bash
make build && make up
curl -s localhost:8000/ | jq        # want "service": "GunnersAnalysis"
curl -s localhost:8000/health | jq  # want a "postgres" key
```

`football-redis` is stale in the same way, but redis is unversioned here so it
does not matter.

---

## 3. `_latest` is a self-referential symlink, and `_index.json` has a phantom match

**Status: FIXED 2026-08-05** — `update_season_index()` now skips `LATEST_LINK`
and any symlink. Both season directories repaired. Regression cover added in
`tests/unit/test_output_layout.py` (11 tests); verified they fail 3/11 with the
fix reverted.



```bash
$ ls -la output/2026-2027/
lrwxrwxrwx  _latest -> _latest          # points at itself
$ jq '.matches[].dir' output/2026-2027/_index.json
"_latest"
"mw17_2026-05-24_crystal-palace_v_arsenal"
```

Both season directories are affected (`2026-2027` and `friendlies-2026`).

### Cause

`visualizer/output_layout.py:100` — `update_season_index()` scans the season
directory and accepts any entry that has a `match.json` inside it:

```python
for name in sorted(os.listdir(season_path)):
    manifest_path = os.path.join(season_path, name, 'match.json')
    if not os.path.isfile(manifest_path):
        continue
```

On the **second** render of a season, `_latest` already exists as a symlink to a
real match directory, so `_latest/match.json` resolves and `_latest` is appended
as an entry. It ties with the real directory on the sort key
`(date, matchweek)`, and Python's sort is stable, so the `sorted(os.listdir())`
order decides the tie — `'_latest'` (`_` = 0x5F) sorts before `'mw17_…'`
(`m` = 0x6D). `entries[0]['dir']` is therefore `'_latest'`, and `_link_latest()`
at line 133 points the link at itself.

### Fix

Skip the link in the scan loop:

```python
for name in sorted(os.listdir(season_path)):
    if name == LATEST_LINK:
        continue
```

`LATEST_LINK` is already defined at line 39. Then re-render one match per season
to rewrite `_index.json` and relink, or just delete the bad links and rebuild:

```bash
rm output/2026-2027/_latest output/friendlies-2026/_latest
make render          # rewrites _index.json and recreates the link
```

Worth adding a regression test alongside — render twice into a temp season
directory and assert `os.path.realpath(_latest)` is a real match directory and
that `_index.json` has no `_latest` entry.

---

## 4. The Airflow webserver dies on startup and takes the UI with it

**Status: FIXED 2026-08-05** — master/worker timeouts raised to 300s and a
healthcheck added to `airflow` and `orchestrator`. Measured cold boot is ~6 min
(the webserver imports every bundled provider), so `start_period` is 420s.
The container now reports `unhealthy` while the webserver is genuinely down
instead of looking `Up`.



The webserver crashed on **2026-08-04 05:39** and was still down 24 hours later:

```
webserver | Running the Gunicorn Server with: Workers: 4 sync, Host: 0.0.0.0:8080
webserver | [2026-08-04T05:39:59] {webserver_command.py:223} ERROR - No response
            from gunicorn master within 120 seconds
webserver | [2026-08-04T05:39:59] {webserver_command.py:224} ERROR - Shutting
            down webserver
```

### Why it is easy to miss

`standalone` runs the webserver, scheduler and triggerer as separate processes.
Only the webserver died, so everything *looked* healthy:

- `docker ps` showed `gunners-airflow` as `Up 24 hours`
- `docker port` showed `8080/tcp -> 0.0.0.0:8080`, and the host was listening
- `airflow dags list` worked, because that talks to the metadata DB
- the scheduler and triggerer kept heartbeating in the logs

But `curl localhost:8080/health` returned `000` — TCP connected, then nothing
came back. Orphaned gunicorn master and worker processes were still sitting in
the process table having never bound to the port.

### Cause

Airflow's `standalone` gives the gunicorn master 120 seconds to respond and
kills the webserver if it does not. On the restart I timed, the webserver took
**~100 seconds** to answer a 200 — a 20-second margin. Any extra load at boot
(the 38 fixture DAGs all parsing at once, a cold page cache) pushes it over.

### Fix

Short term, a restart is enough:

```bash
docker compose restart airflow
make ui        # polls until :8080 really answers 200, then opens a browser
```

Long term, raise the threshold in `docker-compose.yml` under the `airflow`
service's `environment:`:

```yaml
AIRFLOW__WEBSERVER__WEB_SERVER_MASTER_TIMEOUT: "300"
```

Then `make build && make up`. Consider adding a healthcheck to the `airflow`
service too. `postgres`, `airflow-db` and `redis` all have one; `airflow` and
`orchestrator` do not, which is exactly why this went unnoticed for a day —
and why issue 2 went unnoticed for longer:

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -sf localhost:8080/health || exit 1"]
  interval: 30s
  timeout: 10s
  retries: 5
  start_period: 180s
```

### Already done

`make ui` and `make api` were added for this. `make ui` polls `/health` rather
than opening a tab immediately, because opening one right after `make up` lands
on connection-refused while the webserver is still binding.

---

## Fixed already, for the record

Committed in `12cd2b9`, no action needed:

| Was | Now |
|---|---|
| `pytest.ini` had `--cov-report` in `addopts` without `--cov`, so a bare `pytest` aborted with "unrecognized arguments" wherever `pytest-cov` was absent — including system `python3` | coverage flags moved into `make test-cov`; 39 unit tests pass |
| Every documented command said `python …`, which does not exist on this box (only `python3` and `venv/bin/python`) | `Makefile` resolves `./venv/bin/python`, falls back to `python3` |
| `make clean` ran `docker compose down -v`, silently destroying the Postgres volume | `make clean` only removes regenerable files under `data/`; wiping the warehouse is `make reset CONFIRM=1`, which refuses without the flag |
| `COMMANDS.md` referenced `run.sh`, `test_arsenal_match.py`, `visualizer/visualize.py` and a `/pipeline/run` body the API rejects | rewritten against the current API, DAG names and CLI flags; every command in it has been run |

---

---

## Also fixed while working through the above

**`db/loader.py` recorded every quality report as passing.** `quality/checks.py`
returns `checks` as a *list*; the loader assumed a *dict*, so
`len(checks) if isinstance(checks, dict)` gave `checks_total = NULL`,
`failed = 0`, and `passed = (failed == 0)` — **always True, even for a failing
report**. It now reads the report's own `passed` / `checks_total` /
`checks_failed`, falling back to counting only error-severity failures. The DAG
never hit this because `validate` raises first, but a manual load or the
maintenance backfill would have written a lie.

**Stale processed CSVs.** `data/processed/<job>/` still held output from before
the transformer fix — `type`, `outcomeType` and `period` all NaN, `pro` all
zero — so re-running the gate against them reported FAIL. Regenerated: 13/13
checks pass, 81 progressive passes, 3 goals.

**Root-owned `data/`.** 36 files were owned by root from earlier container runs,
which blocked the regeneration above. Fixed with a one-shot
`docker run --rm -v "$PWD/data":/data alpine chown -R <uid>:<gid> /data`.

**Housekeeping from the list at the bottom of this file:** default branch renamed
`master` → `main`; README's broken `final_output/` image replaced with a
committed `docs/assets/dashboard_preview.png` and every other `final_output/`
reference updated to the `output/<season>/<match>/` layout; the xG limitation
rewritten to describe `enrich_xg`; test counts corrected to 50; empty
`tests/integration/` and `tests/fixtures/` removed.

Still open from that list: no repo description or LICENSE on GitHub.

## Housekeeping, low priority

- **Default branch is `master`.** The commit convention in this repo targets
  `main`. Rename if you want them to agree:
  `gh repo edit Muhammadatef/GunnersAnalysis --default-branch main` after
  `git branch -m master main && git push -u origin main`.
- **No repo description or LICENSE** on GitHub.
- **`README.md` still embeds `final_output/00_match_dashboard_unified.png`**,
  and `final_output/` is gitignored and no longer produced — renders now go to
  `output/<season>/…`. The image is broken on GitHub.
- **`tests/integration/` and `tests/fixtures/` are empty directories.** Either
  fill them or drop them; `pytest.ini` declares an `integration` marker nothing
  uses.
- **The stored `quality_reports` row is a ghost from old code.** It reads
  `{"status": "PASS", "message": "Validation skipped for development",
  "checks_total": 0}`, but that string appears nowhere in the current
  codebase — more fallout from issue 2, written by the stale image.
  `quality/checks.py` implements real checks and reports a true
  `checks_total`, so the next load should look different. Worth re-checking
  the row after the rebuild rather than trusting the current one.
