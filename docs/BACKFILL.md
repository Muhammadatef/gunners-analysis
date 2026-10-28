# Backfilling a finished season

The fixture DAGs handle a season as it happens, one match at a time, 30 minutes
after each final whistle. A season that has already been played needs the
opposite shape: 38 matches at once, unattended, against a bot-protected site,
where any single one can fail without costing the other 37.

Two files do it.

| | |
|---|---|
| `airflow/fixtures/build_season_calendar.py` | derives the calendar from SofaScore |
| `orchestrator/backfill_season.py` | ingests it, match by match, resumably |

```bash
make calendar BACKFILL_SEASON='2025/2026'   # once
make backfill-plan                          # what would run, touching nothing
make backfill                               # ~1 hour for 38 matches
make readiness                              # is the result analysable?
```

---

## Why the calendar is derived, not typed

`build_fixtures.py` hand-codes the *upcoming* season from the published fixture
list, because nothing has been played yet and there is nothing to derive from.

A finished season is the opposite: everything about it is public, so typing it
out by hand is both wasted effort and a source of error. `build_season_calendar.py`
walks the club's SofaScore calendar — one polite JSON call per page of 30, no
browser — and writes the same shape `db/seed_fixtures.py` already reads, plus
two keys the forward-looking file has no use for:

**`sofascore_event_id`** — the xG source, resolved now so the ingest never has
to search for it match by match.

**`final_score`** — a free end-to-end assertion. Home and away are positional
all the way through this pipeline, so a transposed fixture produces plausible,
entirely wrong data. Checking the loaded row against a score that came from a
source the pipeline never touched is what catches that.

The club-name map is explicit and an unknown club is a hard error, not a guessed
slug that fails silently three hours into a run. SofaScore calls Liverpool
`Liverpool FC`; that one variant stopped the first 2025/26 build outright, which
is the behaviour working.

## Why WhoScored ids cost one page load, not 38

WhoScored mints a match id a few weeks before kick-off, so the calendar stores a
slug and the id is resolved later. For a *finished* season all 38 already exist,
and Arsenal's fixtures page lists the completed season alongside the current
one — 173 links for 2025/26 in one fetch. `resolve_ids()` maps every slug from
that single page:

```bash
python orchestrator/backfill_season.py --season 2025/2026 --resolve-ids
```

`make calendar` leaves `whoscored_match_id` null; run the above, or let
`backfill` refuse to start and tell you to. A match the fixtures page will not
surface can be pinned by hand — the field is just JSON.

## What the backfill guarantees

**Resume is a query, not a file.** `matches.match_id` *is* the WhoScored match
id, so the set of finished work is `SELECT match_id FROM matches`. There is no
bookkeeping file that can drift from reality. Re-running after a crash, a bot
wall, or a closed laptop picks up exactly what is missing. The ledger under
`data/backfill/` is written for the operator; it is never read back as truth.

**One bad match costs one match.** A failure is recorded and the run continues.
The exit code is non-zero if anything needs another pass, and re-running the
same command retries only those.

**Every match is checked twice, against the row that landed** — not against
what the code thought it did:

1. the score, against the calendar's independent `final_score`
2. xG *and* xGOT, against the requirement that both came from SofaScore

A match that loads its events but not its xG is reported as `no-xg` rather than
counted as done, because WhoScored's feed carries no xG at all and the shot map
and goalkeeper panels would be placeholders.

## Options

```
--dry-run            print the plan, touch nothing
--only 1,2,5-9       restrict to those matchweeks
--force              re-ingest matches already in the warehouse
--pause 20           seconds between matches
--mode xvfb          browser mode; xvfb keeps a long run off your desktop
--resolve-ids        only fill in missing whoscored_match_ids, then exit
--no-seed            skip seeding the calendar into the warehouse
```

`--mode` is worth setting deliberately. The scraper prefers a real window when a
display exists, which on a desktop means an hour of Chrome stealing focus.
`xvfb` is still a real browser with a real renderer — only the display is
virtual — so it keeps the fingerprint out of headless territory.

## Two things that bit, and what changed

**xG was silently absent from 8 of 38 matches.** The check that decides whether
two feeds describe the same match compared club names by token overlap.
WhoScored abbreviates where SofaScore spells out, so `Man Utd` and
`Manchester United` shared no token and the enrichment refused to write. The
same code was wrong in the other direction too: `united` and `city` were
stripped as noise, so both Manchester clubs reduced to `{manchester}` and
matched *each other* — a false positive that would have written one club's xG
onto another club's match without a word. The twenty Premier League clubs are
now compared by explicit key; anything unlisted falls through to the old token
overlap. See `scraper/sofascore.py`.

This also repairs the live path: the fixture DAGs call `enrich_xg` with no URL
at all, so they depend entirely on `find_event` matching both club names.

**The xT grid was re-downloaded once per match.** A fixed 12×8 table, fetched
38 times to ingest a season, and a DNS blip on GitHub failed a match outright.
It is now cached under `data/xT_Grid.csv` after the first download. The failure
mode was at least honest — `_load_xt_grid` raises rather than falling back,
because a zeroed grid would quietly wrong panel 08 and every `xt_per_90` — but
an unrelated network hiccup should not cost a match, least of all during an
unwatched Saturday-night DAG run.

## Another club

Nothing here is Arsenal-specific except two defaults. Point the calendar builder
at another SofaScore team id and the backfill at that club's schema:

```bash
python airflow/fixtures/build_season_calendar.py --season 2025/2026   # team_id=42
python orchestrator/backfill_season.py --season 2025/2026 --club barcelona
```

The WhoScored slug map and the fixtures-page team id are the two things that
need extending. See [MULTI_CLUB.md](MULTI_CLUB.md) for how schemas and output
directories are kept apart.
