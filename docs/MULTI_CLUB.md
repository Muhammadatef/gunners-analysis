# Multi-club analysis

How the pipeline stopped being Arsenal-only, and how it works now.

This document covers the change set that added per-club warehouse schemas, the
two-link manual entry point, and real-browser scraping. For the Arsenal season
automation — which is unaffected — see [AUTOMATION.md](AUTOMATION.md).

---

## Why

The project was built around one club and one competition, and three
assumptions were baked in deep enough to block anything else:

1. **One schema.** Everything lived in `gunners`. Loading a Barcelona match
   meant mixing it into Arsenal's `teams`, `matches` and season views.
2. **One source per run.** WhoScored's Opta feed carries the event stream the 18
   panels are built from but **no xG at all**. SofaScore carries xG and xGOT but
   no pass-by-pass feed. `run_match()` accepted a SofaScore URL only as a
   *fallback* for when WhoScored had nothing — never as a companion — so the
   success path rendered `0.0` in every xG panel.
3. **Headless Chrome.** Which WhoScored increasingly declines to serve.

The goal was to be able to analyse a Champions League night, a Barcelona league
match, an AC Milan derby or an Al Ahly fixture, by hand, from two links, with
each club's data kept separate.

---

## The model

### One club, one schema

Each club gets a complete clone of `db/schema.sql` — its own 14 tables, 4 views,
`fixture_status` enum, and triggers.

```
gunners.matches     gunners.events     gunners.v_team_season_summary
barcelona.matches   barcelona.events   barcelona.v_team_season_summary
ac_milan.matches    ac_milan.events    ac_milan.v_team_season_summary
```

Nothing is shared. Barcelona's row for Real Madrid is a different row from
Arsenal's, and that is deliberate — total isolation, no cross-club foreign keys,
`pg_dump -n barcelona` is a complete backup of one club's world.

This was almost free, because **the schema name appears in exactly three lines
of the codebase**. Every `CREATE TABLE`, every foreign key, every view body and
every query in `db/loader.py`, `orchestrator/api.py` and the DAGs is
*unqualified* and resolves through `search_path`.

| Where | What |
|---|---|
| `db/schema.sql:19-20` | `CREATE SCHEMA IF NOT EXISTS gunners; SET search_path TO gunners, public;` |
| `db/connection.py` | `SET search_path TO <schema>, public` on every connection |

### How a connection picks its schema

`db/connection.py` resolves the target in this order:

```python
connect(schema='barcelona')      # 1. explicit — used by the one true writer
with use_schema('barcelona'):    # 2. contextvar — set once at an entry point
GUNNERS_DB_SCHEMA=barcelona      # 3. environment
'gunners'                        # 4. default — every existing caller unchanged
```

The contextvar is the interesting one. `connect()` is called with no arguments
from roughly ten places, all of them Arsenal paths that must keep resolving to
`gunners`; threading a parameter through all of them would be churn for no gain.
A contextvar set once at the entry point covers everything downstream, and —
unlike an environment variable — it is per-task: FastAPI background tasks run
through `anyio.to_thread.run_sync`, which *copies* the context, so a Barcelona
job and an Arsenal job can be in flight in the same process without clobbering
each other.

The flip side, worth knowing: **a bare `threading.Thread` starts with a fresh,
empty context** and would silently fall back to `gunners`. That is why
`db.loader.load()` — the only thing in the codebase that writes — takes an
explicit `club=` and passes it as `connect(schema=club)` rather than trusting
ambient state.

### Provisioning

`db/schema.sql` is retargeted **in memory**, never on disk:

```python
schema_sql(ddl, 'barcelona')     # rewrites only the two header lines
```

It is never rewritten in place because `docker-compose.yml` bind-mounts the file
into `/docker-entrypoint-initdb.d/`, where it has to stay a valid standalone
`gunners` bootstrap.

The rewrite asserts it matched **exactly one** header and raises `RuntimeError`
otherwise. Without that guard, a `schema.sql` whose header drifted would be
executed verbatim — silently building a club's tables inside `gunners`.

```bash
make schema-club CLUB=barcelona     # explicit
make schema-club CLUB='AC Milan'    # -> schema ac_milan
```

Club names are slugified (`normalise_club`) then validated (`validate_schema`)
against `^[a-z][a-z0-9_]{0,62}$` with `public`, `pg_*` and `information_schema`
rejected. `make match` calls `ensure_schema()` itself, so this is only needed
when you want the tables ahead of the first run.

### What per-club schemas do *not* give you

`fixtures.opponent` and `fixtures.venue` (`'H'`/`'A'`) are defined *relative to
one club* — the schema-per-club model fits that perfectly, but only Arsenal has
a seeded calendar. So in any other schema:

* `v_season_progress` is empty, and `make season SCHEMA=barcelona` returns no rows
* `matches.fixture_id` is `NULL` (the loader already tolerated this)

Everything else — `matches`, `events`, `shots`, `match_player_stats`,
`v_team_season_summary`, `v_player_season_totals` — is fully populated.

---

## Two links, one match

Neither source is complete on its own, so the manual entry point takes both.

```bash
make match CLUB=barcelona \
  WS='https://www.whoscored.com/matches/1234567/live/...' \
  SS='https://www.sofascore.com/football/match/barcelona-chelsea/xyz' \
  COMP='Champions League' MW=6
```

or, with every flag:

```bash
python orchestrator/run_match.py \
  --club barcelona \
  --whoscored '<url>' --sofascore '<url>' \
  --season '2025/2026' --competition 'Champions League' --matchweek 6 \
  --min-events 500 --home-color '#A50044' --away-color '#FEBE10' \
  --mode headed --no-load
```

### Stage order

```
scrape ─→ extract ─→ transform ─→ enrich ─→ validate ─→ render ─→ load
   │          │                      │          │                  │
WhoScored  matchCentreData      SofaScore    hard gate        <club> schema
                                 xG/xGOT
```

`enrich` is the new step. The old success path skipped it entirely, which is why
xG was always `0.0`. It now runs between transform and the quality gate, so the
panels render real numbers.

### The sides check

This is the part that matters most, and the reason the enrich step is more than
four lines.

Home/away identity is **positional everywhere in this codebase** — it comes from
dict insertion order in `transformer/transform.py`, not from a flag — and until
now nothing cross-referenced the two feeds. Paste the SofaScore link for the
wrong match, or the right match listed the other way round, and you would get
confident, plausible, *wrong* xG written into `matches.home_xg` with no signal
at all.

So before writing anything, `_sides_swapped()` compares SofaScore's home/away
against WhoScored's:

| Case | Result |
|---|---|
| Both sides agree | written as-is, `xg_source: sofascore` |
| Sides are reversed | swapped, `xg_source: sofascore (sides swapped)`, logged |
| Neither matches | **nothing written**, `xg_source: unavailable: sofascore teams do not match (Inter v Napoli)` |

Enrichment is never fatal: a SofaScore outage, a league with no xG, or a
mismatched link all leave the metadata intact and record what happened in
`xg_source`, which is carried through to `matches.xg_source` in the warehouse.

### Event resolution

Three ways in, in order:

1. an explicit `sofascore_event_id`
2. `resolve_event_id(url)` — the manual path
3. `find_event(date, home, away)` — **Arsenal only**

That third one walks a single club's SofaScore calendar and defaults to
`team_id=42`, which is Arsenal. `run_match` gates it on the club for exactly
that reason: for Barcelona it would find nothing at best, and a same-date
coincidence at worst.

The full SofaScore bundle — shot map, lineups, incidents, statistics, momentum
graph, average positions, managers — is saved to
`data/raw/sofascore_<job>.json` for later analysis, and the xG is computed from
that one fetch rather than a second round of HTTP.

### Competition and colours

Two things WhoScored's payload does not carry, both of which default to
Arsenal/Premier League:

* **`--competition`** — without it a Champions League tie is filed as
  `Premier League` in `matches.competition` *and* in the output directory name.
* **`--home-color` / `--away-color`** — the curated colour map covers the 20
  Premier League clubs only, so a Barcelona report without them renders in
  placeholder colours.

### The quality gate

`quality/checks.py` is calibrated to a Premier League match (~1200–2000 Opta
events) and is a **hard gate** — a failure stops the run before rendering.
Checking the thresholds against other shapes:

| Check | Non-PL behaviour |
|---|---|
| `covers_full_match` (≥ 90') | already extra-time safe |
| `event_count_in_range` max 3000 | clears a 120-minute match (~2500) |
| `eleven_starters_per_team` | holds for friendlies and cup ties |
| `event_count_in_range` **min 800** | **the one that bites** a sparser feed |

So there is one flag, `--min-events`, rather than a config file. The thresholds
actually used are recorded in the report and stored in `quality_reports.report`,
so a run is self-describing after the fact.

---

## Scraping with a real browser

Headless Chrome announces itself in a dozen small ways — a `HeadlessChrome`
user-agent token, `navigator.webdriver`, no GPU, no plugin surface, no input
events ever — and WhoScored's front door notices.

### Mode ladder

```
--mode / GUNNERS_BROWSER_MODE / auto:
    headed      real window        ($DISPLAY is set)
    xvfb        real Chrome, virtual display   (Xvfb available)
    headless    last resort
```

`headed` and `xvfb` both run **real Chrome with a real renderer**; only the
display differs. Containers have no X server, so `docker-compose.yml` pins them
to `xvfb`. **For a genuinely visible window, run on the host with `LOCAL=1`.**

### What else changed in the scraper

* **The pinned user-agent is gone.** It claimed `Chrome/120` from a `Chrome/151`
  binary — a mismatch checkable in one line. Chrome's own UA always matches its
  own TLS and JS fingerprint.
* `--disable-gpu` only in headless mode; `--no-sandbox` only in a container.
* `excludeSwitches: ['enable-automation']`, `useAutomationExtension: False`, and
  a CDP script that unsets `navigator.webdriver` before any page script reads it.
* **A persistent profile** at `data/browser-profile/`, so cookies and any consent
  or challenge clearance survive between runs. Delete it to start clean.
* Randomised viewport, a mouse move and a scroll between load and read.
* **`pageLoadStrategy='eager'` and a 45s page-load timeout.** This is the one that
  fixed a real failure: a Betis friendly hung for **300 seconds** inside
  Selenium's own renderer timeout before failing. It now resolves in ~20s.
* Polling for `matchCentreData` instead of a flat `sleep(wait_time)` — a good page
  returns in a couple of seconds, a blocked one fails at the deadline.
* One retry with a fresh browser and a jittered 5–15s backoff.

### What this does not solve

Bot walls are made *less likely*, not impossible. If WhoScored serves a
challenge, the persistent profile helps on subsequent runs but the first may
still fail. Requests are paced, not parallelised — one match page at a time,
which is the other half of not looking like a bot.

### Diagnosing a failed scrape

`_diagnose()` in `orchestrator/pipeline.py` separates three cases that look
identical from the outside:

| Signal | Meaning |
|---|---|
| `_Incapsula_Resource`, `captcha`, `Access Denied` | bot wall — retrying later usually works |
| under 50 KB | truncated download — retry |
| `initialMatchDataForScrappers` present, no `matchCentreData` | **WhoScored published the match but not its event data.** Common for friendlies and lower tiers. Retrying will never help. |

That last case is what the SofaScore fallback exists for: `run_match` catches it
and renders the reduced nine-panel report instead of failing outright.

---

## Where things land

### On disk

```
output/
  2026-2027/                                    Arsenal — the flat layout
    _index.json                                 every render, newest first
    _latest -> mw17_2026-12-26_...
    mw17_2026-12-26_crystal-palace_v_arsenal/
      00_dashboard.png
      match.json
      panels/01_home_pass_network.png ...

  barcelona/                                    every other club, one level deeper
    2025-2026/
      _index.json                               its own index and _latest
      mw06_2025-11-26_barcelona_v_chelsea/
```

Arsenal stays flat because that is where the DAGs have always written; nesting
it would split one club's index in two. `run_match` handles this explicitly
rather than by magic in `output_layout`.

Working files stay job-keyed and shared across clubs — they are UUID-named, so
there is no collision, but note that `make clean` wipes every club's:

```
data/raw/match_<job>.html          scraped page
data/raw/match_<job>.json          extracted matchCentreData
data/raw/sofascore_<job>.json      the SofaScore bundle
data/processed/<job>/              EventData.csv, PlayerData.csv, metadata.json
data/quality/<job>_report.json     the quality report
data/browser-profile/              Chrome profile (gitignored)
```

### Finding a render again

Match directories are named from the fixture, not the job, so `match.json`
records the `job_id` and `output_layout.find_match_dir(job_id)` walks the tree
to find it. This is what `/visualizations/{job_id}` uses — it checks the season
layout first, then the flat `data/visualizations/<job>` that `run_visualize`
still writes, and returns the `match_dir` alongside the file names.

---

## Scheduled runs moved to the final whistle

The Arsenal fixture DAGs used to fire at kick-off + 4 hours. They now fire **30
minutes after the final whistle**.

The whistle is derived, not observed — no feed tells us when a match actually
ended — so it is kick-off + `MATCH_DURATION_MINUTES` (default 115: 90 minutes,
a 15-minute interval, and ~10 of stoppage across both halves). The run is that
plus `POST_MATCH_DELAY_MINUTES` (default 30), so the default schedule is
**kick-off + 2h25**.

```
20:00 kick-off  →  21:55 whistle  →  22:25 run   (cron: 25 22 21 8 *)
15:00 kick-off  →  16:55 whistle  →  17:25 run   (cron: 25 17 29 8 *)
```

This is deliberately tight against WhoScored's own processing lag — the match
centre is not always populated the moment the whistle goes. What absorbs that:

* `retries=6` at `retry_delay=30min` — roughly three hours of runway
* `end_date = run_at + 3 days` — the window stays open
* `arsenal_season_maintenance` (Mondays 06:00 UK) backfills anything that still
  missed

**Raise `POST_MATCH_DELAY_MINUTES` if first attempts fail often enough to be
noise.** Extra time is not modelled: a cup tie going to 120 minutes simply fires
early and retries.

`PIPELINE_DELAY_HOURS` is superseded but still honoured — if it is set it wins,
and behaves exactly as before, as a total offset from kick-off. It has been
removed from `docker-compose.yml`, where it was previously pinned to `4` and
would otherwise have overridden the new default.

---

## The two entry points

Both execute the same `pipeline.run_match`.

| | `orchestrator/run_match.py` | `POST /pipeline/run` |
|---|---|---|
| Shape | synchronous, non-zero exit on failure | fire-and-forget, poll `/pipeline/status/{job_id}` |
| Good for | a manual run you want to watch | dashboards, scripts, queued work |
| Browser mode | `--mode` | whatever the container is configured for |
| Club | `--club` (required) | `club` field (optional, defaults to Arsenal) |

The API used to *reimplement* the stage sequence inline, which is how the
SofaScore fallback ended up unreachable over HTTP despite existing in
`run_match`. `_execute` is now a single `run_match` call with an `on_stage`
callback that writes progress into redis — which is what the module docstring
already claimed.

Read endpoints take `?club=` to query another club's schema.

---

## Backward compatibility

Nothing about the Arsenal path changed:

* `gunners` is still the default schema for every caller that does not ask
* the 38 fixture DAGs and the maintenance DAG are untouched apart from a
  `scraper.close()` in a `finally` (the scraper no longer quits its own driver
  on success)
* `make schema`, `make seed`, `make render`, `make load`, `make season`,
  `make table`, `make players` behave identically; the query targets gained a
  `SCHEMA` variable that defaults to `gunners`
* `enrich_xg(job_id)` keeps its positional signature, so the DAG's call is unchanged
* `run_visualize()` and its flat `data/visualizations/<job>/` output still exist
  for the API and the maintenance DAG
* `MIN_EVENTS` and friends remain module constants; `Thresholds()` defaults to them

---

## Verified

* `make schema-club CLUB=barcelona` → 14 tables, 4 views, the `fixture_status`
  enum; idempotent on re-run
* a real match loaded into `barcelona` (1410 events, views populated) with
  `gunners` unchanged — no cross-schema leakage
* headed scrape of a Premier League match: 1.66 MB, `matchCentreData` present
* live SofaScore URL → event id → bundle → real xG
* the Betis friendly that hung for 300s now fails cleanly in ~20s with the
  correct diagnosis
* all 39 DAGs load with no import errors; a 20:00 kick-off schedules at 22:25
* `/visualizations/{job_id}` resolves a real job to its season directory and
  lists 19 PNGs
* 180 unit tests pass

---

## Known gaps

* **No fixture calendar for non-Arsenal clubs.** `build_fixtures.py` is a
  hand-typed Arsenal/PL generator. Other clubs load with a `NULL fixture_id`,
  which works, but `v_season_progress` stays empty for them.
* **`TEAM_COLORS` is Premier League only** — 20 clubs. Other clubs need explicit
  colour flags. A club registry (slug, Opta id, SofaScore team id, colours)
  would fix this, `find_event`'s Arsenal default, and crest lookup in one go.
* **No shot-level xG join.** SofaScore's per-shot `xg`/`xgot` are in the saved
  bundle but not matched onto WhoScored's `shots` rows — there is no player-id
  mapping between Opta and SofaScore, so it needs a fuzzy matcher.
* **`teams` and `players` are duplicated per schema.** Intentional (isolation
  over normalisation), but it does mean the same player carries a row in every
  club schema they appear in.

---

## Reference

| Thing | Where |
|---|---|
| Schema selection, validation, provisioning | `db/connection.py` |
| The pipeline stages and `run_match` | `orchestrator/pipeline.py` |
| The CLI | `orchestrator/run_match.py` |
| Browser modes and fingerprint flags | `scraper/scraper.py` |
| SofaScore client, `xg_from_bundle`, `find_event` | `scraper/sofascore.py` |
| Output paths, `find_match_dir` | `visualizer/output_layout.py` |
| Quality thresholds | `quality/checks.py` |
| Commands | [../COMMANDS.md](../COMMANDS.md) |
| Why WhoScored is primary | [DATA_SOURCES.md](DATA_SOURCES.md) |
| Schema reference and query recipes | [DATABASE.md](DATABASE.md) |
