# Season warehouse

Defined in [`db/schema.sql`](../db/schema.sql): 14 tables, 4 views, one enum,
three `updated_at` triggers.

**One club, one schema.** Arsenal is `gunners`; every other club gets a complete
clone of the same objects — `barcelona.matches`, `ac_milan.events` — so nothing
is ever mixed. Nothing in the DDL or in any query is schema-qualified; it all
resolves through `search_path`. See [MULTI_CLUB.md](MULTI_CLUB.md) for the
mechanics.

```bash
# apply / re-apply (idempotent)
python db/connection.py [--schema gunners | --club barcelona]

# seed the 2026/27 calendar (Arsenal only - no other club has one)
python db/seed_fixtures.py

# load a rendered match
python db/loader.py --job <job_id> --season 2026/2027 [--fixture-key gw17_...] [--club barcelona]
```

Connection comes from `GUNNERS_DB_URL`
(default `postgresql://gunners:gunners@postgres:5432/gunners`). The schema is
*not* in the DSN — it is chosen per connection, defaulting to `gunners`.

Every query below is written against `gunners.`; swap the prefix for another
club, or `SET search_path TO barcelona, public` once and drop it entirely.

---

## Grain

| Table | One row per | Typical count |
|---|---|---|
| `seasons` | season | 1 |
| `teams` | club | 20 |
| `players` | player | ~500/season |
| `player_team_spells` | player x club x season | handles mid-season transfers |
| `fixtures` | scheduled match | 38 |
| `matches` | played + ingested match | 1 per fixture |
| `match_team_stats` | team x match | 2 |
| `match_player_stats` | player x match | ~40 |
| `events` | Opta event | ~1400 |
| `shots` | shot | ~25 |
| `pass_links` | passer→receiver pair x match | ~267 |
| `visualizations` | rendered PNG | 19 |
| `quality_reports` | validation run | 1 |
| `pipeline_runs` | pipeline execution | 1+ |

Counts are from the Crystal Palace 1–2 Arsenal reference match.

---

## Key tables

### `fixtures`

The season calendar and the state machine that drives Airflow.

```
fixture_key           gw17_2026-12-26_crystal-palace_v_arsenal   (natural key)
matchweek             17
scheduled_date        2026-12-26
kickoff_uk            NULL until the TV slot is confirmed
kickoff_confirmed     false
venue                 'H' | 'A'   (always from Arsenal's point of view)
opponent              'Crystal Palace'
whoscored_slug        england-premier-league-2026-2027-crystal-palace-arsenal
whoscored_match_id    resolved a few weeks before kick-off
status                fixture_status enum
attempts, last_error  backfill bookkeeping
```

`status` values: `scheduled → resolving → ready → ingesting → ingested`, plus
`failed` and `postponed`.

### `matches`

Result, context, and the xG values that WhoScored does not supply.

`home_xg`/`away_xg`/`home_xgot`/`away_xgot` are nullable and default to `0.0`
from `metadata.json`. `xg_source` records where they came from, so a later
backfill can tell real numbers from placeholders:

| `xg_source` | Meaning |
|---|---|
| `sofascore` | summed from SofaScore's shot map (or its team statistics) |
| `sofascore (sides swapped)` | as above, but SofaScore listed the teams the other way round and the values were transposed to match |
| `fotmob` | team xG/xGOT from FotMob (used when SofaScore's API is blocked) |
| `fotmob (sides swapped)` | as above, with home/away transposed to match WhoScored |
| `manual` | supplied by hand on the CLI or in the API request |
| `unavailable: …` | why it could not be filled — no event matched, no xG in that competition, or the SofaScore link described a different match |
| `NULL` | never attempted |

### `match_team_stats`

Two rows per match, holding exactly what the Match Stats and Shot Map panels
render: possession, field tilt, passes, long balls, crosses, corners, goalkick
length, progressive passes, zone-14 and half-space passes, take-ons, shots,
shots on target, big chances, average shot distance, tackles, interceptions,
clearances, aerials, ball recoveries, high turnovers, plus the two derived
heights (`def_action_height_m`, `pass_line_height_m`).

### `events`

The full Opta stream with coordinates already scaled to 105 x 68 and the derived
columns attached: `xt_start`, `xt_end`, `xt_delta`, `progression_m`,
`is_progressive`. `qualifiers` is `JSONB` with a GIN index, so qualifier lookups
stay fast:

```sql
SELECT count(*) FROM gunners.events
WHERE match_id = 1903353 AND qualifiers @> '[{"type":{"displayName":"KeyPass"}}]';
```

### `shots`

Denormalised from `events` for xG work. `xg` is nullable and currently unset —
the features a model needs (`distance_m`, `body_part`, `situation`,
`is_big_chance`, `is_header`, `goal_mouth_y/z`) are already populated.

---

## Views

### `v_team_season_summary` — running league table

```sql
SELECT team_name, played, won, drawn, lost,
       goals_for, goals_against, points, avg_possession, total_xt
FROM gunners.v_team_season_summary
ORDER BY points DESC, goal_difference DESC;
```

```
   team_name    | played | won | drawn | lost | goals_for | goals_against | points | avg_possession | total_xt
----------------+--------+-----+-------+------+-----------+---------------+--------+----------------+----------
 Arsenal        |      1 |   1 |     0 |    0 |         2 |             1 |      3 |          60.22 |     1.35
 Crystal Palace |      1 |   0 |     0 |    1 |         1 |             2 |      0 |          39.78 |     0.99
```

### `v_player_season_totals` — totals and per-90s

```sql
SELECT player_name, minutes, goals, assists, key_passes,
       progressive_passes, xt_per_90, avg_rating
FROM gunners.v_player_season_totals
WHERE team_name = 'Arsenal'
ORDER BY xt_per_90 DESC NULLS LAST
LIMIT 10;
```

### `v_season_progress` — calendar vs reality

```sql
SELECT matchweek, scheduled_date, venue, opponent, status,
       home_score, away_score, panels_rendered
FROM gunners.v_season_progress;
```

### `v_team_match_results` — per-match result rows

Feeds the other two views; useful directly for form curves.

```sql
SELECT match_date, opponent_team_id, result, points, xg, xt
FROM gunners.v_team_match_results
WHERE team_id = 13 ORDER BY match_date;
```

---

## Recipes

**Arsenal's form over the last five**

```sql
SELECT match_date, result, goals, goals_conceded, possession_pct
FROM gunners.v_team_match_results
WHERE team_id = 13
ORDER BY match_date DESC LIMIT 5;
```

**Most progressive passers per 90 (min 300 minutes)**

```sql
SELECT player_name, minutes, progressive_passes, prog_passes_per_90
FROM gunners.v_player_season_totals
WHERE team_name = 'Arsenal' AND minutes >= 300
ORDER BY prog_passes_per_90 DESC;
```

**Strongest passing partnerships across the season**

```sql
SELECT p1.name AS passer, p2.name AS receiver, SUM(l.pass_count) AS passes
FROM gunners.pass_links l
JOIN gunners.players p1 ON p1.player_id = l.passer_id
JOIN gunners.players p2 ON p2.player_id = l.receiver_id
WHERE l.team_id = 13
GROUP BY 1, 2 ORDER BY passes DESC LIMIT 15;
```

**Shot profile by situation**

```sql
SELECT situation, count(*) shots, count(*) FILTER (WHERE is_goal) goals,
       round(avg(distance_m), 1) avg_distance
FROM gunners.shots WHERE team_id = 13
GROUP BY situation ORDER BY shots DESC;
```

**Where the pipeline is struggling**

```sql
SELECT fixture_key, status, attempts, left(last_error, 120)
FROM gunners.fixtures
WHERE status IN ('failed', 'ingesting') ORDER BY scheduled_date;
```

**Ingest timings**

```sql
SELECT dag_run_id, stage, status, duration_s
FROM gunners.pipeline_runs ORDER BY started_at DESC LIMIT 20;
```

---

## Re-ingesting

Loading the same job twice is safe and leaves row counts unchanged:

- reference tables upsert on their natural key
- child tables are deleted by `match_id` and re-inserted in the same transaction

To force a full re-render and reload:

```bash
python visualizer/match_report.py --job <job_id> [--club barcelona]
python db/loader.py --job <job_id> --season 2026/2027 --fixture-key gw17_... [--club barcelona]
```

To drop a match entirely, `DELETE FROM gunners.matches WHERE match_id = …` —
every child table cascades.

---

## Backup

```bash
# one club
docker compose exec postgres pg_dump -U gunners -n gunners gunners | gzip > gunners_$(date +%F).sql.gz
docker compose exec postgres pg_dump -U gunners -n barcelona gunners | gzip > barcelona_$(date +%F).sql.gz

# everything
docker compose exec postgres pg_dump -U gunners gunners | gzip > warehouse_$(date +%F).sql.gz
```

Because each club's schema is self-contained — no cross-schema foreign keys —
`pg_dump -n <club>` is a complete, restorable backup of that club on its own.
