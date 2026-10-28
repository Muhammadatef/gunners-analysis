# Repository audit and production readiness

Two questions: what in here is dead weight, and what still stands between this
and something you'd trust unattended for 38 fixtures.

---

## Part 1 — Files that are dead or stale

Nothing below has been deleted. Verified by grepping for imports and references
across `*.py`, `Dockerfile`, `*.yml`, `Makefile` and `*.sh`.

### A. Superseded code — safe to delete

| File | Status |
|---|---|
| `visualizer/visualize.py` | **No Python file imports it.** Superseded by `match_report.py` |
| `visualizer/visualize_old.py` | Same. Referenced only in docs, as history |
| `visualizer/visualize_v2.py` | Same |
| `visualizer/visualize_empty.py` | Same |

These four are the generations that produced the empty pass network and the
three-row match-stats panel. ~1,600 lines of dead code.

### B. Documentation sprawl — 11 files saying the same thing

`FINAL_STATUS.md`, `FINAL_SUCCESS_STATUS.md`, `MISSION_COMPLETE.md`,
`SUCCESS.md`, `SYSTEM_READY.md`, `START_HERE.md`, `WAKE_UP.md`, `SUMMARY.md`,
`READ_ME_FIRST.txt`, `DASHBOARD_WITH_DATA_READY.txt`, `VIEW_DASHBOARD.txt`

All describe the **pre-Airflow, pre-warehouse, pre-SofaScore** state. Several
actively mislead — they point at `visualize.py`, claim "18 plots working" from
the era when the panels were empty, and describe a `final_output/` flat layout
that is no longer how output is organised.

Superseded by `README.md` + `docs/`.

### C. Stale artifacts

| File | Issue |
|---|---|
| `filestructure.txt` | A snapshot that now includes `__pycache__` and `final_output_sofascore`; regenerate or drop |
| `preview.webp` | Not referenced anywhere |
| `TODO.md` | Predates everything built since; items are done or obsolete |
| `test_arsenal_match.py` | Hits `localhost:8000` with the **old** `PipelineRequest` schema (`date`, `league`, `gameweek`, mandatory xG). That contract changed — this script will 422 against the current API |
| `run.sh` | Its only real action is `python3 test_arsenal_match.py`, so it inherits the same breakage |
| `final_output/` + `final_output_sofascore/` | Superseded by `output/<season>/<match>/`. Kept so nothing you had is lost |

### D. Keep, despite appearances

| File | Why |
|---|---|
| `quality/validate_strict.py` | Imported by `quality/validate.py`; holds the pandera schemas the tests exercise |
| `Post_Match_Report.ipynb` | 5.4 MB, but it is the reference implementation the port is validated against |
| `Makefile` | `build/up/down/logs/test/clean` still work against the current compose file |
| `contracts/` | Used by `transformer/transform.py` |

### Suggested cleanup

```bash
# dead visualisers
git rm visualizer/visualize.py visualizer/visualize_old.py \
       visualizer/visualize_v2.py visualizer/visualize_empty.py

# stale status docs
git rm FINAL_STATUS.md FINAL_SUCCESS_STATUS.md MISSION_COMPLETE.md SUCCESS.md \
       SYSTEM_READY.md START_HERE.md WAKE_UP.md SUMMARY.md READ_ME_FIRST.txt \
       DASHBOARD_WITH_DATA_READY.txt VIEW_DASHBOARD.txt TODO.md filestructure.txt

# broken smoke test + its wrapper (or rewrite against the new API)
git rm test_arsenal_match.py run.sh
```

That removes ~2,000 lines of dead code and 11 misleading documents. **The repo is
not under git**, so make a copy first — `git init` would be the better first step.

---

## Part 2 — What's left for production

Ordered by what would actually bite first.

### Blocking

**1. ~~Nothing has run end-to-end on a real fixture.~~ Closed 2026-08-07** by
backfilling all 38 matches of 2025/26 — `resolve → scrape → transform →
enrich_xg → validate → visualize → load`, unattended, against live WhoScored and
SofaScore. See [BACKFILL.md](BACKFILL.md).

What that shook out, none of which unit tests could have found:

- xG was silently absent wherever WhoScored abbreviated a club name and
  SofaScore did not (`Man Utd` / `Manchester United`) — 8 of 38 matches. The
  same comparison also matched the two Manchester clubs *to each other*.
- A real `0.0` xG was written as NULL, because `M.hxg or None` cannot tell a
  genuine zero from the 0.0 default. Three of the first ten matches had a side
  with no shots on target.
- The xT grid was re-fetched from GitHub once per match; one DNS blip failed a
  match outright.

All three would have hit the live 2026/27 season too — the fixture DAGs run the
same stages. **The remaining untested path is the DAG wrapper itself**: the
schedule window, `resolve_match_id` against an unplayed fixture, and the retry
and backfill behaviour. MW1 (Coventry, 21 Aug) is still the first live test of
that layer, and still worth watching manually.

**2. No alerting.**
If a DAG fails at 23:00 on a Saturday, nothing tells you. `pipeline_runs` and
`fixtures.last_error` hold everything a notifier needs. A Slack/email webhook on
`on_failure_callback` is perhaps an hour of work and is the highest-value missing
piece.

**3. Secrets are defaults.**
`POSTGRES_PASSWORD=gunners`, `AIRFLOW_PASSWORD=admin`, and a `.env` that predates
the warehouse. Also `.env` is committed alongside `.env.example`.

**4. No backups.**
A season of event data in a single Docker volume with no dump schedule. One
`docker compose down -v` and it's gone.

### Important

**5. Scraper fragility.** Selenium against a bot-protected site is the weakest
link. Mitigations exist (6 retries at 30 min, weekly backfill) but there is no
proxy rotation and no alert distinguishing "blocked" from "no data".

**6. SofaScore matches don't reach the warehouse.** The schema is event-grained;
a SofaScore match has no `events` or `pass_links`. Needs a `matches.source`
column and the event tables allowed to be empty.

**7. Airflow runs `standalone`.** Fine for one team, but it is not a
production topology — no separate scheduler/webserver, no restart policy, SQLite-
style single-process assumptions.

**8. Data ownership.** Files under `data/` are root-owned from earlier container
runs; a non-root process cannot overwrite them. Fix with `chown` and a consistent
`AIRFLOW_UID`.

**9. Test coverage is uneven.** 231 tests cover scheduling, quality checks, the
transformer, the output layout, xG enrichment, schema routing, the backfill's
selection and gates, and `db/aggregate.py`'s xG handling. There are still **no
tests** for `db/loader.py` end to end, or for `db/aggregate.py` beyond the xG
columns — the row builders take a fully constructed `M`, which needs a fixture
match to exercise honestly.

### Nice to have

**10. Own xG model.** `shots.xg` exists and the features are populated
(distance, body part, situation, big chance). Currently dependent on SofaScore.

**11. Player dashboards.** Notebook cells 44–56 hold a 2×3 player layout never ported.

**12. Season-level visuals.** `v_player_season_totals` and `v_team_season_summary`
exist but nothing renders form curves or rolling xG.

**13. Automated insight reports.** `REPORT.md` in the MW17 folder was written by
hand from the warehouse; the queries behind it are repeatable and could be
templated per match.

### Honest summary

The **pipeline** is production-ready in structure: idempotent loads, a real
quality gate, source fallback, retries, backfill, verified scheduling.

The **operation** is not: no alerting, no backups, default passwords, and zero
live runs. Items 1–4 are a day's work and would move this from "works on my
machine" to "runs a season unattended".
