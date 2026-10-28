# Visual output

## Where renders land

`final_output/` was fine for a single match; across a 38-game season it would be
overwritten every week. Every match now gets its own directory:

```
output/
  2026-2027/                                 Arsenal - the flat layout
    _index.json                              every match rendered, newest first
    _latest -> mw17_2026-05-24_...           symlink to the most recent render
    mw01_2026-08-21_arsenal_v_coventry/
      00_match_dashboard_unified.png
      match.json                             score, source, stats, panel list
      panels/01_home_pass_network.png ... 18_away_chance_creating.png
  friendlies-2026/
    f_2026-08-01_girona-fc_v_arsenal/
      00_match_dashboard_sofascore.png
      match.json
      panels/01_shot_map.png ... 09_away_territory.png
  barcelona/                                 every other club, one level deeper
    2025-2026/
      _index.json                            its own index and _latest
      mw06_2025-11-26_barcelona_v_chelsea/
```

Directory names sort in fixture order, and re-rendering a match overwrites only
its own folder. Both the WhoScored and SofaScore renderers use the same layout,
so `match.json` tells you which source produced a given report - and its
`job_id`, which is how `output_layout.find_match_dir()` (and
`/visualizations/{job_id}`) finds a render again.

Arsenal stays at the top level because that is where the DAGs have always
written; nesting it would split one club's index in two.

```bash
python visualizer/match_report.py --season 2026/2027 --matchweek 17
python visualizer/match_report.py --club barcelona --season 2025/2026
python visualizer/sofascore_report.py --bundle <bundle.json>
python visualizer/match_report.py --out data/visualizations/x   # legacy flat layout
```

`GUNNERS_OUTPUT_ROOT` overrides the root; `--output-root` does the same per run.

---

# The 18 panels (WhoScored)

`visualizer/match_report.py` is a port of `Post_Match_Report.ipynb`. It reads the
scraped match JSON, rebuilds every panel, and writes 18 individual PNGs plus a
6 x 3 unified dashboard with both club crests.

```bash
python visualizer/match_report.py                       # newest scraped match
python visualizer/match_report.py --job <job_id> --club barcelona
python visualizer/match_report.py --only dashboard      # or --only panels
```

Layout matches the notebook: home team down the left column, away down the
right, match-wide panels in the middle.

| # | File | Column | What it shows |
|---|---|---|---|
| 1 | `01_home_pass_network` | home | Nodes at each player's median event position, edges weighted by completed passes between the pair. Circle = starter, square = sub. Dashed line = median pass height |
| 2 | `02_shot_map` | match | Both teams' shots mirrored toward the centre, plus a seven-row stat bar (goals, xG, xGOT, shots, on target, xG/shot, average distance) |
| 3 | `03_away_pass_network` | away | As 1, axes inverted so the away team attacks right to left |
| 4 | `04_home_defensive_block` | home | KDE heatmap of defensive actions, node size by volume, dashed line at average defensive-action height |
| 5 | `05_gk_performance` | match | Both goalmouths seen from behind, shots plotted by `goalMouthY`/`goalMouthZ`, with xGOT faced, saves, conceded, goals prevented |
| 6 | `06_away_defensive_block` | away | As 4, inverted |
| 7 | `07_home_progressive_passes` | home | Comet lines for passes gaining ≥ 9.144 m toward goal, split left / centre / right with counts and percentages |
| 8 | `08_match_momentum` | match | Average xT per minute, home above the axis and away below, with goal markers |
| 9 | `09_away_progressive_passes` | away | As 7, inverted |
| 10 | `10_home_danger_zones` | home | Passes ending in Zone 14 (orange) or the half-spaces (team colour), counted in two hexagons |
| 11 | `11_match_stats` | match | Eleven metrics head to head: possession, field tilt, passes, long balls, corners, goalkick length, tackles, interceptions, clearances, aerials, ball recoveries |
| 12 | `12_away_danger_zones` | away | As 10, inverted |
| 13 | `13_home_pass_end_zones` | home | Positional bin heatmap of where completed passes finish, labelled as a share of all passes |
| 14 | `14_high_turnovers` | match | Possession won within 40 m of the opponent goal, both teams on one pitch |
| 15 | `15_away_pass_end_zones` | away | As 13, inverted |
| 16 | `16_home_chance_creating` | home | Key passes (violet) and assists (green) over a count heatmap of their origin |
| 17 | `17_crosses_analysis` | match | Every open-play cross, green successful / red not, counted by wing |
| 18 | `18_away_chance_creating` | away | As 16, inverted |

`00_match_dashboard_unified.png` is all 18 on one 3120 x 4964 canvas with the
header band.

---

## The header

Every panel and the dashboard carry the same header: home crest, scoreline,
competition / matchweek / date / venue / half-time score, then a summary line of
xT, possession, shots and xG.

Crests live in `visualizer/assets/`, catalogued in `assets/logos.json`, and are
looked up by `club_key` — the same canonicalisation the xG check uses — so
WhoScored's `Man Utd` and SofaScore's `Manchester United` find the same file.
All 20 clubs of the 2025/26 and 2026/27 fields resolve.

To add a club: drop the file in `visualizer/assets/`, add an entry to
`logos.json`, and add the club to `_CANONICAL` in `scraper/sofascore.py` if it
is not there. A club with no asset renders without a crest rather than failing.

Two things to know, both learned the hard way:

- **The file has to be the format its name claims.** `matplotlib.image.imread`
  picks its decoder from the extension, so a WebP named `.png` raises
  `SyntaxError: not a PNG file` — which failed a whole match at the render
  stage, after the scrape, transform, enrichment and quality gate had all
  passed. Five shipped assets were exactly that. `tests/unit/test_crests.py`
  now asserts every asset really is a PNG.
- **Deriving the filename from the team name does not work.** It used to, and
  because WhoScored abbreviates, almost every crest silently resolved to
  nothing.

---

## Where the port diverges from the notebook

Three deliberate differences, all in the direction of correctness:

1. **Pass network edges use successful passes only.** The notebook counts every
   pass, then relies on an inner join to drop links whose "receiver" was an
   opponent. Most survive that filter, but not all — an unsuccessful pass
   followed by a team-mate's ball recovery creates a link that never happened.
2. **xG per shot uses each team's own shot count.** The notebook divides the away
   team's xG by the *home* team's shot total (`axGpSh = round(axg/hTotalShots, 2)`),
   which is a straightforward typo.
3. **Momentum is computed on a copy.** The notebook multiplies the away team's xT
   by −1 in place, so the panel is only correct on odd-numbered runs — a caveat
   the notebook itself documents. Here the sign flip happens on a copy, so the
   output is deterministic.

Everything else — thresholds, colours, marker sizes, text placement, the mirrored
shot map, the inverted away axes — follows the notebook.

---

## Data sources and caveats

- **Events**: WhoScored / Opta, via the scraped `matchCentreData` payload.
- **xT grid**: mckayjohns 12 x 8 grid, cached at `visualizer/assets/xT_Grid.csv`
  so rendering works offline.
- **xG / xGOT**: *not* in the WhoScored feed. They are read from
  `metadata.json` and default to `0.0`, which means the xG row of the shot map
  and the "xGOT faced / goals prevented" figures on the GK panel are placeholders
  until real values are supplied. Everything else on those panels is computed
  from the event stream and is real.

---

## Colours

Team colours come from `metadata.json` (`home_color` / `away_color`), populated
at transform time from `visualizer/palette.py`. Background and line colours are
`bg_color` / `line_color`, defaulting to white on black — matching the notebook.

### Both teams are never the same colour

Every panel encodes team identity as colour, and panels 02 and 11 put both
teams in one figure, where colour is the only thing separating them.

Arsenal are `#EF0107`. Six Premier League clubs wear a red that is effectively
the same mark: Bournemouth and Manchester United (`#DA291C`), Sunderland
(`#EB172B`), Brentford (`#E30613`), Nottingham Forest (`#DD0000`) and Liverpool
(`#C8102E`). Arsenal play each twice, so **twelve of a season's thirty-eight
reports** drew both passing networks, both defensive blocks and a shared shot
map in one indivisible red.

`palette.resolve(home, away)` prevents that. It measures the pair the way the
eye works — Euclidean distance in OKLab (×100), against the plain colours and
against protanope, deuteranope and tritanope simulations — and requires **≥ 15
normal-vision** and **≥ 8 under any deficiency**. Arsenal red against
Bournemouth red scores 4.0 and 1.1.

When a pair fails, the clubs change strip, as they would on a Saturday:

1. the **away** side moves to its second colour (`TEAM_ALT_COLORS`)
2. failing that, the **home** side moves to its own
3. failing that, both
4. last resort, a neutral `#3D3D3D` — deliberately not another hue, so it reads
   as "not the other team" rather than pretending to be a kit

So Arsenal at home to Bournemouth stays red against Bournemouth's black; away
at Old Trafford, Arsenal switch to their own navy `#023474` and United keep
their red. A colour passed to `run_match` as `--home-color` / `--away-color` is
honoured as the starting point but still moved if the pair would be unreadable
— an illegible chart is not what the caller asked for either.

The thresholds and the CVD matrices are the standard data-visualisation ones;
`tests/unit/test_palette.py` covers every red-on-red fixture in both directions.

---

## Posting the visuals

Every render writes two files per image: the full-resolution `.png` and a
`_x.jpg` sibling. **Post the `_x.jpg`.** The PNG is the archive copy, and while
`make_png_upload_safe` keeps it legal too, only a lossy format can trade quality
for bytes when a match renders denser than usual.

```
mw04_2026-09-12_sunderland_v_arsenal/
  00_match_dashboard_unified.png      archive - full resolution
  panels/02_shot_map.png              archive - one PNG per panel
  linkedin/00_match_dashboard_unified.jpg   post this one
  linkedin/02_shot_map.jpg                  ... and these
```

`visualizer/social_export.py` proves every file against four limits before
writing it, and `check_upload_safe()` is the same predicate if you want to
re-check by hand:

| limit | value | which platform |
|---|---|---|
| alpha channel | must be absent | both |
| width / height | 0.8 .. 3.0, and never *exactly* on either bound | LinkedIn |
| pixel box | 7680 x 4320 | LinkedIn |
| file size | 3 MB | LinkedIn (X allows 5) |

The last two are there because LinkedIn enforces them **silently**. An image
over 4320 px tall, or sitting at precisely the 4:5 floor, is accepted by the
composer and then simply never appears in the post — no error, no placeholder.
The unified dashboard natively renders 3984x4973, which is why it was the one
that vanished. Anything that fails here fails at the moment of posting, so it is
worth re-running the check rather than trusting the eye:

```bash
python -c "from visualizer.social_export import check_upload_safe as c; \
           from pathlib import Path; \
           [print(p.name, c(p)) for p in Path('output/2026-2027/_latest').rglob('linkedin/*.jpg')]"
```

---

## Superseded files

The four earlier generations (`visualize.py`, `visualize_old.py`,
`visualize_v2.py`, `visualize_empty.py`) produced the empty-node pass network and
three-row match-stats panel that `match_report.py` replaced. They were deleted in
the repository cleanup and remain recoverable from the initial git commit.
