# Data sources

What each source actually publishes, and why the pipeline is built the way it is.

The 18-panel dashboard needs **event-level data with start and end coordinates
for every pass**. That is a much higher bar than "has xG" or "has match stats",
and it is the single fact that decides which sources are usable.

## Verdict

| Source | Full event stream + coords | xG | Current PL | Cost | Verdict |
|---|---|---|---|---|---|
| **WhoScored (Opta)** | **yes** (~1400 events/match) | no | yes | free | **primary** |
| **SofaScore** | no - aggregates, shot map, heatmaps | **yes** | yes | free | **fallback** |
| Understat | shots only | yes | yes (6 leagues) | free | xG cross-check |
| FBref | no - and advanced stats pulled Jan 2026 | no longer | yes | free | not usable |
| StatsBomb Open Data | yes, richest of all | yes | **no** | free | historical only |
| StatsBomb / Opta / Wyscout API | yes | yes | yes | paid | out of scope |

## The detail

### WhoScored - primary

Embeds Opta's `matchCentreData` in the match page: ~1400 events per match, each
with type, outcome, qualifiers, and x/y (plus endX/endY on passes). This is the
only free source that supports all 18 panels for a current Premier League match.

Caveats: Selenium-based scraping is bot-sensitive, and **coverage is not
universal** - friendlies and lower-tier competitions get a summary feed with no
`matchCentreData` at all. `orchestrator/pipeline.extract_match_json` distinguishes
that case from a bot wall, because only one of them is worth retrying.

No xG anywhere in the feed.

### SofaScore - fallback

Public JSON API, no browser needed. Per match: shot map with coordinates, team
statistics (**including xG**), per-player aggregates across ~60 stat keys,
per-player touch heatmaps, average positions, and a momentum graph.

What it does **not** have is a pass-by-pass feed. So `visualizer/sofascore_report.py`
renders nine panels, not eighteen - anything needing pass start/end coordinates
(pass network edges, progressive passes, zone 14, pass end zones, chance-creating
arrows, crosses, high turnovers) is absent rather than faked.

Worth noting: SofaScore's xG is the one thing it has that WhoScored lacks, so it
is useful as a *supplement* even when WhoScored works.

### Understat - xG cross-check only

Shot-level data with coordinates and xG for six leagues (EPL, La Liga,
Bundesliga, Serie A, Ligue 1, RFPL). Never had a full event feed. As of this
writing the match pages expose only a `match_info` summary block
(`h_xg`, `a_xg`, shots, shots on target) to a plain HTTP fetch; the shot arrays
that used to be inline are no longer served that way.

Useful if you want a second xG opinion. It cannot drive the dashboard.

### FBref - no longer usable

FBref's advanced statistics were **removed in January 2026** after Stats Perform
terminated its data agreement with Sports Reference. xG, progressive passes and
shot-creating actions are gone. Even before that, FBref published aggregated
tables, never event coordinates.

### StatsBomb Open Data - historical only

The richest free event data that exists (~3400 events per match, freeze frames,
360 data). Verified against `competitions.json`: 80 competition-seasons across 24
competitions - but Premier League coverage is **2003/04 and 2015/16 only**.
Excellent for model training or historical work; useless for this season.

## Combining sources (the default for Premier League matches)

The two free sources are complementary rather than competing: WhoScored has the
event stream and no xG; SofaScore has xG and no event stream. The pipeline uses
**both** for a normal fixture.

```
resolve → scrape (WhoScored) → transform → enrich_xg (SofaScore → FotMob) → validate → visualize → load
```

`enrich_xg` tries SofaScore first (shot map + xGOT). When that API returns 403
(common from Docker/datacenter IPs), it falls back to FotMob's public match API
for the same team-level xG/xGOT fields. `matches.xg_source` records which feed
won (`sofascore`, `fotmob`, or `unavailable: …`).

Two ways in, and which one you get depends on the club:

| Path | How the event is found | Used by |
|---|---|---|
| **By URL** | `resolve_event_id(url)` - you paste the SofaScore link alongside the WhoScored one | `run_match.py`, `POST /pipeline/run` - the only option for a non-Arsenal club |
| **By search** | `find_event(date, home, away)` walks one club's SofaScore calendar, defaulting to Arsenal's team id | the fixture DAGs, where the club is fixed |

The whole SofaScore bundle - shot map, lineups, incidents, statistics, momentum,
average positions - is saved to `data/raw/sofascore_<job>.json`, and the xG comes
out of that single fetch.

**The two links must describe the same match.** Before writing anything, the two
feeds' home/away sides are compared: agreeing sides are written as-is, reversed
sides are transposed and marked `sofascore (sides swapped)`, and a genuinely
different match writes nothing and says which teams it saw. Home/away identity is
positional throughout this pipeline, so without that check a mispasted link would
produce plausible, wrong xG with no signal.

Verified on Crystal Palace 1-2 Arsenal:

| | WhoScored alone | + SofaScore |
|---|---|---|
| xG | `0.8 - 2.5` (placeholder) | **`0.98 - 2.54`** |
| xGOT | `0.6 - 2.1` (placeholder) | **`0.95 - 1.71`** |
| Shots | 8 - 17 | 8 - 17 (agrees) |
| Possession | 39.8% - 60.2% | 40% - 60% (agrees) |

The shot and possession agreement across two independent providers is a useful
sanity check on the transform.

The step is best-effort: if the match cannot be matched, or the competition has
no xG, the run continues and `xg_source` records why.

## How the pipeline chooses

```
scrape WhoScored
      │
      ├── matchCentreData present ──► 18 panels + full warehouse load
      │
      └── NoEventDataError
              ├── bot wall / truncated  ──► retry (Airflow, 6x at 30 min)
              └── no event data published ──► SofaScore fallback, 9 panels
```

`orchestrator.pipeline.run_match(job_id, whoscored_url, season, sofascore_url=...)`
implements this. Without a `sofascore_url` it raises rather than silently
producing a thinner report.

## If full event data is essential for every match

There is no free source. The realistic options are a paid feed (StatsBomb, Opta /
Stats Perform, Wyscout) or accepting the reduced SofaScore report for the handful
of fixtures WhoScored does not cover. For Premier League matches - the actual use
case here - WhoScored coverage has been complete.
