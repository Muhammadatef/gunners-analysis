# The Football Data Cookbook

A practical reference for anyone entering football analytics from a data
background. Every definition here is implemented in this repository, and every
number is from one real match — **Crystal Palace 1-2 Arsenal, 24 May 2026** —
so you can trace any concept from theory to code to output.

If you know SQL and Python but not football data, start at section 1 and read
straight through. If you know football but not the data, skip to section 4.

---

## Table of contents

1. [The four levels of football data](#1-the-four-levels-of-football-data)
2. [Anatomy of an event](#2-anatomy-of-an-event)
3. [Coordinate systems — the thing everyone gets wrong](#3-coordinate-systems--the-thing-everyone-gets-wrong)
4. [Event types: the full taxonomy](#4-event-types-the-full-taxonomy)
5. [Qualifiers: where the detail actually lives](#5-qualifiers-where-the-detail-actually-lives)
6. [Core team metrics](#6-core-team-metrics)
7. [Advanced metrics: xG, xGOT, xT](#7-advanced-metrics-xg-xgot-xt)
8. [Progression metrics](#8-progression-metrics)
9. [Spatial concepts: zone 14, half-spaces, danger zones](#9-spatial-concepts)
10. [Defensive metrics](#10-defensive-metrics)
11. [Player metrics and per-90 normalisation](#11-player-metrics-and-per-90-normalisation)
12. [Network metrics: passing partnerships](#12-network-metrics-passing-partnerships)
13. [Reading a match from the numbers](#13-reading-a-match-from-the-numbers)
14. [Ten ways to fool yourself](#14-ten-ways-to-fool-yourself)
15. [The data source landscape](#15-the-data-source-landscape)
16. [Building the pipeline](#16-building-the-pipeline)
17. [Glossary](#17-glossary)
18. [Learning path](#18-learning-path)

---

## 1. The four levels of football data

Football data comes in tiers. Knowing which tier you have determines what
questions you can answer — and most confusion in this field comes from people
assuming they have a higher tier than they do.

| Level | What it is | Rows per match | Can answer |
|---|---|---|---|
| **1 · Result** | Score, date, teams | 1 | League tables, form, basic prediction |
| **2 · Match aggregate** | Possession %, shots, corners, cards | ~30 numbers | "Who dominated?" |
| **3 · Event** | Every on-ball action with x/y coordinates | **~1,400** | "*How* did they dominate? Where? Through whom?" |
| **4 · Tracking** | All 22 players + ball, 25 times/second | **~3,000,000** | Off-ball runs, pressing shape, space creation |

This project works at **level 3**. That is the sweet spot for public analytics:
rich enough for real tactical insight, and obtainable for free.

**The critical distinction:** level 3 is *on-ball only*. You know where the ball
went, not where the other 21 players were. You can see that a pass split the
defence; you cannot see the striker's decoy run that created the gap. Level 4
answers that, and it is expensive and almost never public.

```
Level 2:  "Arsenal had 60% possession"
Level 3:  "Arsenal had 60% possession, 23 passes into Zone 14, and their
           threat collapsed to near zero after going 2-0 up"
Level 4:  "...because the two forwards stopped making the runs that had been
           dragging Palace's centre-backs apart"
```

---

## 2. Anatomy of an event

An **event** is one on-ball action, recorded by a human analyst watching video
(companies like Opta employ hundreds of them). Here is a real pass, exactly as
it arrives from the feed:

```json
{
  "minute": 0, "second": 0,
  "teamId": 13, "playerId": 467918,
  "x": 50.0, "y": 50.0,
  "endX": 46.0, "endY": 49.4,
  "type":        {"value": 1, "displayName": "Pass"},
  "outcomeType": {"value": 1, "displayName": "Successful"},
  "qualifiers":  [{"type": {"displayName": "Longball"}}, ...]
}
```

### The universal fields

| Field | Meaning | Note |
|---|---|---|
| `minute`, `second` | Clock time | `minute` restarts logic per half; use `expandedMinute` for continuous |
| `period` | FirstHalf / SecondHalf / ExtraTime… | Always filter on this for half-splits |
| `teamId`, `playerId` | Who did it | Join to squad list; `playerId` is null for team events |
| `x`, `y` | **Where it started** | See section 3 — this is the field people misuse |
| `endX`, `endY` | **Where it ended** | Passes and carries only |
| `type` | What happened | See section 4 |
| `outcomeType` | Successful / Unsuccessful | Meaning is type-dependent! |
| `qualifiers` | Everything else | See section 5 |
| `isTouch`, `isShot`, `isGoal` | Convenience booleans | |

### `outcomeType` is not what you think

This trips up everyone. "Successful" means *successful for the event type*, not
"good for the team":

- `Pass` + Successful → reached a team-mate
- `Tackle` + Successful → won the ball
- **`Aerial` + Successful → won the header**
- **`Foul` + Successful → you were fouled** (Unsuccessful = you committed it)
- `TakeOn` + Successful → beat your man

Getting `Foul` backwards is a classic first-week bug.

### In this project

Events land in the `events` table, one row each, with derived columns attached:

```sql
SELECT minute, type, outcome, x, y, end_x, end_y,
       xt_delta, progression_m, is_progressive
FROM gunners.events WHERE match_id = 1903353 ORDER BY event_index;
```

---

## 3. Coordinate systems — the thing everyone gets wrong

**This is the single biggest source of silent errors in football analytics.**

### Raw providers use a normalised grid

The feed gives coordinates on a **0–100 × 0–100** grid. Not metres. Not a real
pitch shape. A "10 unit" pass across the width is not the same distance as a
"10 unit" pass along the length.

```
(0,100) ─────────────────────────────── (100,100)
   │                                        │
   │  own goal                opponent goal │
   │     ◄── attacking direction ──►        │
   │                                        │
(0,0) ───────────────────────────────── (100,0)
```

### Always scale to a real pitch before measuring distance

A standard pitch is **105 m × 68 m**. This project scales on ingest:

```python
df['x'] = df['x'] * 1.05    # 0-100  →  0-105 metres
df['y'] = df['y'] * 0.68    # 0-100  →  0-68  metres
```

Skip this and every distance, angle and "progressive pass" count is wrong.
You will not get an error — you will get plausible-looking nonsense.

### Both teams attack left → right

Providers normalise so each team's events are recorded attacking **towards
x = 100**, regardless of which way they kicked. This means:

- To draw both teams on one pitch, **mirror one of them**: `(105 − x, 68 − y)`
- To compare "how deep did they defend", you can use `x` directly
- Never assume raw `x` tells you which end of the actual stadium

### Goal-mouth coordinates are a *different* system

Shots carry extra fields describing where the ball crossed the goal line:

| Field | Meaning |
|---|---|
| `goalMouthY` | Lateral position across the goal |
| `goalMouthZ` | **Height** above the ground |

These are on their own scale, unrelated to pitch x/y. They are what makes a
goalkeeper "shot placement" chart possible.

### Sanity check your scaling

```sql
SELECT min(x), max(x), min(y), max(y) FROM gunners.events WHERE match_id = 1903353;
-- expect 0.0, 105.0, 0.0, 68.0
```

If you see 100 as a maximum, you forgot to scale. If you see 68 as an x
maximum, you swapped the axes.

---

## 4. Event types: the full taxonomy

Every event type present in one real match, with counts. **Note that passes are
65% of all events** — this ratio is a useful data-quality check.

### On-ball attacking

| Type | Count | Meaning |
|---|---|---|
| `Pass` | **920** | Any intentional ball to a team-mate |
| `TakeOn` | 32 | Attempted dribble past an opponent |
| `BallTouch` | 56 | Touch that isn't a pass/shot (incl. miscontrol) |
| `Dispossessed` | 9 | Lost the ball to a challenge |
| `ShieldBallOpp` | 2 | Shielded it out of play |

### Shots — four separate types, not one

| Type | Count | Meaning |
|---|---|---|
| `Goal` | 3 | Went in |
| `SavedShot` | 13 | Keeper saved it **or an outfielder blocked it** |
| `MissedShots` | 8 | Off target |
| `ShotOnPost` | 1 | Hit the woodwork |

> **Trap:** there is no single "Shot" type. Total shots =
> `Goal + SavedShot + MissedShots + ShotOnPost`. And `SavedShot` includes
> *blocked* shots, which are **not** shots on target. To get true shots on
> target you must exclude the blocked ones — this project filters on qualifier
> value 82:
> ```python
> saved = shots[(shots.type == 'SavedShot') & (~shots.qualifiers.str.contains(': 82,'))]
> ```

### Defensive

| Type | Count | Meaning |
|---|---|---|
| `BallRecovery` | 69 | Picked up a loose ball (no duel) |
| `Clearance` | 51 | Hoofed it away from danger |
| `Aerial` | 46 | Header duel — **logged for both players** |
| `Tackle` | 19 | Challenged a dribbler |
| `Challenge` | 19 | **Was beaten** by a dribbler (the inverse of Tackle) |
| `BlockedPass` | 17 | Intercepted a pass with the body |
| `Interception` | 15 | Read and cut out a pass |
| `Error` | 1 | Mistake leading to a chance |

> **Trap:** `Aerial` appears **twice per duel** — once for the winner, once for
> the loser. Counting raw aerials double-counts. Palace and Arsenal both show
> 23 aerials here because it is the same 23 duels seen from both sides.

### Goalkeeper

| Type | Count | Meaning |
|---|---|---|
| `Save` | 12 | Shot stopped |
| `KeeperPickup` | 16 | Collected the ball |
| `Claim` | 1 | Caught a cross |
| `Punch` | 1 | Punched clear |
| `KeeperSweeper` | 1 | Acted outside the box |

### Match administration (filter these out of analysis)

`Start`, `End`, `SubstitutionOn`, `SubstitutionOff`, `FormationSet`,
`FormationChange`, `CornerAwarded`, `Card`, `Foul`, `OffsideGiven`,
`OffsidePass`, `OffsideProvoked`

> **Trap:** `CornerAwarded` is not a corner *taken*. The corner itself is a
> `Pass` with a `CornerTaken` qualifier. Counting `CornerAwarded` and corner
> passes gives you two different, both-defensible numbers.

---

## 5. Qualifiers: where the detail actually lives

The `type` field tells you *what*. **Qualifiers tell you everything that
matters.** One match contained **84 distinct qualifier types**:

| Qualifier | Count | What it gives you |
|---|---|---|
| `Zone` | 1282 | Coarse pitch zone label |
| `PassEndX` / `PassEndY` | 972 | Pass destination |
| `Length` | 971 | Pass distance |
| `Angle` | 971 | Pass direction (radians) |
| `Longball` | 116 | Long pass flag |
| `Chipped` | 89 | Lofted |
| `HeadPass` | 45 | Headed pass |
| `Cross` | 31 | Delivery from wide |
| `ThrowIn` | 28 | Restart |
| `IntentionalAssist` | 28 | Pass intended to create a shot |
| `GoalMouthY` / `GoalMouthZ` | 25 | Shot placement |
| `RightFoot` / `LeftFoot` / `Head` | 92 | Body part |

### The ones that carry the most analytical weight

| Qualifier | Why it matters |
|---|---|
| **`KeyPass`** | The pass that directly created a shot — the basis of "chances created" |
| **`IntentionalGoalAssist`** | An actual assist |
| **`BigChance`** | Analyst-judged "should have scored" — the best free proxy for chance quality |
| **`Corner`** / **`ThrowIn`** / **`Freekick`** | Set-piece flags — **exclude these from open-play analysis** |
| **`Throughball`** | Line-breaking pass |
| **`Blocked`** (value 82) | Distinguishes a real save from a block |

### Working with them

Qualifiers arrive as a nested list. This project stores them as `JSONB` with a
GIN index:

```sql
SELECT count(*) FROM gunners.events
WHERE match_id = 1903353
  AND qualifiers @> '[{"type":{"displayName":"KeyPass"}}]';
```

> **Rule of thumb:** almost every serious metric requires filtering by
> qualifier. "Passes" is nearly meaningless; "open-play passes into the final
> third, excluding set pieces" is a metric.

---

## 6. Core team metrics

All formulas below are implemented in `db/aggregate.py`.

### Possession %

```python
possession = team_passes / (team_passes + opponent_passes) * 100
```

**Palace 39.8% — Arsenal 60.2%**

Note this is the *pass-share* definition, not time-on-ball. Broadcasters use
time; most event-data work uses pass share because event data has no clock for
possession. They usually land within a couple of points. Say which you used.

### Field tilt % — better than possession

```python
final_third_touches = touches where x >= 70   # 105m pitch
field_tilt = team_f3 / (team_f3 + opp_f3) * 100
```

**Palace 35.7% — Arsenal 64.3%**

Field tilt asks "who had the ball *where it matters*". A team can pass the ball
along its own back line all day and post 60% possession without threatening
anyone. Field tilt catches that. **When possession and field tilt diverge
sharply, that gap is the story.**

### Pass accuracy

```
Palace   280 / 366 = 76.5%
Arsenal  484 / 554 = 87.4%
```

Interpret with care: a team playing long, direct balls will always show lower
accuracy. That is a stylistic choice, not incompetence. Pair it with long-ball
share.

### Shot metrics

| Metric | Palace | Arsenal |
|---|---|---|
| Shots | 8 | 17 |
| On target | 3 | 7 |
| Big chances | 3 | 8 |
| Avg. shot distance | 12.89 m | 15.52 m |

**Average shot distance is a quality signal.** Closer is usually better —
though a team taking only close-range shots may be failing to shoot at all from
range.

---

## 7. Advanced metrics: xG, xGOT, xT

These three get conflated constantly. They measure **different things at
different moments**.

### xG — Expected Goals

> **The probability that a shot becomes a goal**, based on historical shots from
> similar positions.

Inputs: distance, angle, body part, assist type, defensive pressure, whether it
was a big chance.

- A shot worth **0.35 xG** goes in about 35% of the time
- Team xG = sum of its shots' xG
- **Measured at the moment of the shot.** It says nothing about what happened before.

```
Palace 0.98  —  Arsenal 2.54
```

Arsenal scored 2 from 2.54 xG — almost exactly par. Palace scored 1 from 0.98 —
also par. The scoreline was fair.

> **Trap:** xG is meaningless in small samples. One match tells you little; ten
> matches tell you a lot. Never say "they deserved to win" from a single game's
> xG — say "they created better chances".

### xGOT — Expected Goals On Target

> Like xG, but computed **after** the shot, using where it ended up in the goal.

Only on-target shots have xGOT. A shot into the top corner has far higher xGOT
than the same shot straight at the keeper.

```
Palace 0.95  —  Arsenal 1.71
```

**xGOT − xG = finishing quality.** **Goals − xGOT = goalkeeping quality.**
Arsenal's xG 2.54 → xGOT 1.71 means their shots were, on balance, placed *worse*
than average for their positions.

### xT — Expected Threat

> **How much a pass or carry increased the chance of scoring** — regardless of
> whether a shot followed.

The pitch is split into a grid (this project uses Karun Singh's **12 × 8**
grid). Each cell has a value: the probability that possession there leads to a
goal within the next few actions.

```
xT of a pass = value(destination cell) − value(origin cell)
```

```
Palace 0.99  —  Arsenal 1.35
```

**Why xT matters:** xG only rewards shots. A brilliant pass that puts a striker
through, who then squares it and the move breaks down, generates **zero xG** but
substantial xT. xT credits *territorial progress and chance creation*, not just
the final action.

| | Measures | When | Credits |
|---|---|---|---|
| **xG** | Shot quality | At the shot | The shooter |
| **xGOT** | Shot placement | After the shot | The finisher/keeper |
| **xT** | Ball progression | Every pass/carry | Build-up players |

> This project computes xT itself from the grid. It has **no xG model** — xG and
> xGOT are merged in from a second provider, because the primary feed doesn't
> carry them.

### Implementation

```python
xT_grid = load('xT_Grid.csv')       # 12 cols x 8 rows
start = xT_grid[y_bin(y_start)][x_bin(x_start)]
end   = xT_grid[y_bin(y_end)][x_bin(x_end)]
xt    = end - start
```

Applied only to **successful open-play passes** — corners and throw-ins are
excluded, because set pieces distort positional value.

---

## 8. Progression metrics

Moving the ball towards goal is the core act of attacking football. Three ways
to measure it:

### Progressive passes

The most common definition, and the one used here:

```python
progressive = (
    type == 'Pass'
    and outcome == 'Successful'
    and x > 42                                # started beyond own third-ish
    and distance_to_goal_before
      - distance_to_goal_after >= 9.144       # gained 10 yards toward goal
)
```

Distance is measured to the **goal centre (105, 34)**, not to the goal line —
so a sideways pass across the top of the box gains nothing, correctly.

**9.144 m is exactly 10 yards.** Different providers use different thresholds
(some use 25% closer, some 30 m in own half / 10 m in final third). **Always
state your definition.**

```
Palace 29  —  Arsenal 51
```

### Final third entries

Count of passes ending with `endX >= 70`. Simple, robust, easy to explain.

### Line-breaking passes

Passes with the `Throughball` qualifier — deliberately played between defenders.
Rarer and higher value.

---

## 9. Spatial concepts

Football pitches are not uniform. Some areas are far more valuable than others,
and the vocabulary below is standard in the industry.

```
                    ATTACKING THIRD (x >= 70)
   ┌────────────────────────────────────────────────┐
68 │                                                │
   │            LEFT HALF-SPACE                     │
56 ├────────────────────────────────────────────────┤
   │        │                                       │
45 │        │        ZONE 14        │      BOX      │
   │        │   (x 70-88.5,         │               │
   │        │    y 22.7-45.3)       │               │
23 │        │                                       │
   ├────────────────────────────────────────────────┤
11 │            RIGHT HALF-SPACE                    │
 0 │                                                │
   └────────────────────────────────────────────────┘
   70                                              105
```

### Zone 14

The central area just outside the penalty box. Named from a 1990s study that
numbered the pitch 1–18 and found **zone 14 generated disproportionately more
assists than any other**. It is where a playmaker turns and picks a pass.

```python
zone14 = (70 <= endX <= 88.54) and (22.66 <= endY <= 45.32)
```

```
Palace 6  —  Arsenal 23
```

### Half-spaces

The vertical channels between the centre and the touchline. A player receiving
here can see the whole goal and both passing lanes, while defenders must decide
whether the centre-back or full-back steps out — which is exactly what creates
the gap.

```python
half_space = endX >= 70 and (11.33 <= endY <= 22.66 or 45.32 <= endY <= 56.95)
```

```
Palace 22  —  Arsenal 43
```

### Why these two numbers told the story of this match

Arsenal made **23 Zone 14 entries to Palace's 6**, and **43 half-space passes to
22**, while attempting **fewer crosses** (13 v 18). Against a compact defence they
deliberately declined the wide route and attacked the seams. That is a tactical
conclusion you simply cannot reach from level-2 data.

### High turnovers

Winning the ball close to the opponent's goal — the payoff of a high press:

```python
high_turnover = (
    type in ('BallRecovery', 'Interception')
    and x >= 70
    and distance_to_opponent_goal <= 40
)
```

```
Palace 1  —  Arsenal 3
```

---

## 10. Defensive metrics

Defensive data is **much weaker** than attacking data, for one structural
reason: **event data only records actions**. A defender who positions himself so
well that no pass is attempted generates *no events*. Great defending is
frequently invisible.

Read defensive numbers with that caveat always in mind.

| Metric | Palace | Arsenal | Note |
|---|---|---|---|
| Tackles (won) | 12 (4) | 7 (3) | **33% success is alarming** |
| Interceptions | 10 | 5 | Reading the game |
| Clearances | 24 | 27 | High = under pressure |
| Aerials won | 11 | 12 | Of 23 duels each |
| Ball recoveries | 34 | 35 | Loose balls |
| Def. action height | 43.10 m | 39.56 m | Where they defended |

### The counter-intuitive one

**More tackles usually means worse defending.** A team making many tackles is a
team constantly being dribbled at. Possession-dominant sides post low tackle
counts because opponents rarely have the ball.

Palace made 12 tackles and won 4. Against a side attempting 19 take-ons, engaging
and losing repeatedly is how the edge of your box gets opened.

### Defensive action height

The average x of a team's defensive actions — a proxy for how high they defended.

> **Careful:** Palace's 43.1 m is *higher* than Arsenal's 39.6 m, which looks
> backwards given Arsenal dominated. It isn't a bug. Arsenal pressed high (3 high
> turnovers to 1), but Palace bypassed the press with long balls, so most of
> Arsenal's actual defending happened deep against balls already over the press.
> **Always cross-check one metric against another before drawing conclusions.**

### PPDA — Passes Per Defensive Action

The standard pressing-intensity metric, not implemented here but worth knowing:

```
PPDA = opponent passes in their own 60% of the pitch
       ÷ (your tackles + interceptions + challenges + fouls there)
```

**Lower = more intense pressing.** Elite pressing sides run 7–9; passive sides 15+.

---

## 11. Player metrics and per-90 normalisation

### The cardinal rule

**Never compare raw totals between players with different minutes.** A
substitute with 2 key passes in 15 minutes is not worse than a starter with 3 in
90. Normalise:

```sql
metric_per_90 = metric * 90.0 / NULLIF(minutes_played, 0)
```

This project exposes that in a view:

```sql
SELECT player_name, minutes, goals, assists,
       prog_passes_per_90, xt_per_90, avg_rating
FROM gunners.v_player_season_totals
WHERE team_name = 'Arsenal'
ORDER BY xt_per_90 DESC;
```

> **Caveat:** per-90 becomes unreliable below ~300 minutes. A player with 45
> minutes and one goal reads as "2.0 goals per 90". Always show the minutes
> alongside, and set a minimum threshold.

### What to look at by position

| Position | Metrics that matter |
|---|---|
| **Goalkeeper** | Saves, goals prevented (xGOT − goals conceded), pass accuracy under pressure, goal-kick length |
| **Centre-back** | Progressive passes, pass accuracy, aerials won, clearances, errors |
| **Full-back** | Crosses (accurate), progressive passes, take-ons, final-third touches |
| **Defensive mid** | **Progressive passes**, pass volume, interceptions, tackles, xT |
| **Attacking mid** | Key passes, xT, Zone 14 receptions, through balls |
| **Winger** | Take-ons won, crosses, key passes, box touches |
| **Striker** | xG, shots, big chances, touches in box, shot distance |

### A real example

```
Martín Zubimendi (DM)  84 touches · 77 passes · 13 progressive · xT 0.208
Gabriel Martinelli (LW) 51 touches · 34 passes ·  4 progressive · xT 0.366
```

Zubimendi moved the ball most; Martinelli created the most threat with far fewer
touches. **Different jobs, both essential, and neither is visible from a
possession stat.**

---

## 12. Network metrics: passing partnerships

Treat the team as a graph: players are nodes, passes are weighted edges.

```sql
SELECT p1.short_name AS passer, p2.short_name AS receiver, pass_count
FROM gunners.pass_links l
JOIN gunners.players p1 ON p1.player_id = l.passer_id
JOIN gunners.players p2 ON p2.player_id = l.receiver_id
WHERE match_id = 1903353 AND team_id = 13
ORDER BY pass_count DESC LIMIT 4;
```

```
Mosquera  → Zubimendi   21
Hincapié  → Martinelli  14
Zubimendi → Madueke     14
Hincapié  → Mosquera    11
```

### How to build one correctly

1. **Use successful passes only.** An unsuccessful pass has no receiver — the
   next event is usually an opponent, so counting it invents links.
2. **Node position = median, not mean.** One long ball drags a mean badly.
3. **Edge width ∝ pass count**, node size ∝ involvement.
4. Consider cutting at the **first substitution** — after that you are averaging
   two different shapes into one misleading picture.

### What to read from it

- **A dominant edge** = a dependency. Mosquera→Zubimendi at 21 passes is the
  single heaviest link; press it and Arsenal's progression suffers.
- **An isolated node** = a player disconnected from the structure.
- **Average node height** = how high the team built (Arsenal 61.95 m,
  Palace 57.23 m).

---

## 13. Reading a match from the numbers

A repeatable order of operations. Applied to Palace 1-2 Arsenal:

**1. Start with the result vs the underlying performance**
```
1-2. xG 0.98 - 2.54. Arsenal created far more, scored roughly to par.
```

**2. Establish who controlled the ball, and where**
```
Possession 40/60, field tilt 36/64. Arsenal controlled it in the right areas.
```

**3. Find the route to goal**
```
Zone 14: 6 v 23. Half-spaces: 22 v 43. Crosses: 18 v 13.
→ Arsenal attacked centrally and declined the wide route.
```

**4. Check the time profile — this is where most insight hides**

xT per 15-minute block:

| Period | Arsenal | Palace |
|---|---|---|
| 15–30 | **0.402** | 0.055 |
| 45–60 *(after going 2-0 up)* | **0.041** | **0.232** |
| 75–90 | **0.023** | **0.261** |

```
→ Arsenal's threat collapsed after scoring. Palace's two best spells sit
  exactly in those windows. The 88th-minute goal was a pattern, not luck.
```

**5. Identify the individuals**
```
Zubimendi 13 progressive passes; Martinelli top xT; Madueke top rated.
```

**6. State what you would change**
```
Arsenal: game management after scoring. Palace: 40% long-ball accuracy and
33% tackle success are both unsustainable.
```

**The lesson:** aggregate numbers said "comfortable Arsenal win". The *time
series* said "Arsenal repeatedly invited pressure". Step 4 is where event data
earns its keep.

---

## 14. Ten ways to fool yourself

1. **Forgetting to scale coordinates.** Every distance metric silently wrong.
2. **Counting `Aerial` events raw.** Logged for both players — you double.
3. **Treating `SavedShot` as shots on target.** Includes blocked shots.
4. **Not excluding set pieces.** Corners wreck pass networks and xT.
5. **Comparing raw totals across different minutes.** Always per-90.
6. **Reading one match's xG as truth.** Needs ~10 matches to stabilise.
7. **Assuming more tackles = better defending.** Usually the opposite.
8. **Ignoring game state.** A team 2-0 up plays differently. Splitting by
   scoreline is often mandatory.
9. **`outcomeType` on fouls.** Successful = you *were* fouled.
10. **Trusting a dashboard you never validated.** In this repo, a transformer
    bug made `type`, `outcomeType` and `period` all null. Every filter silently
    matched nothing and the panels rendered blank-but-plausible. **Hence the
    13-check quality gate that now blocks the pipeline.**

---

## 15. The data source landscape

| Source | Event coords? | xG? | Cost | Use |
|---|---|---|---|---|
| **Opta / Stats Perform** | Yes | Yes | £££ | Industry standard |
| **StatsBomb** | Yes, richest | Yes | £££ (free historical) | Best open data, but old seasons |
| **Wyscout** | Yes | Yes | ££ | Scouting |
| **Public scraping** | Sometimes | Rarely | Free | What this project uses |

**Free options, honestly assessed:**

- **StatsBomb Open Data** — genuinely excellent, ~3,400 events/match with freeze
  frames. But Premier League coverage is 2003/04 and 2015/16 only. Perfect for
  *learning*, useless for current seasons.
- **Public match-centre feeds** — full event streams with coordinates for current
  seasons. This is the only free route to level-3 data for a live league.
- **Aggregate APIs** — good for xG and team stats, no pass-level events.

See [DATA_SOURCES.md](DATA_SOURCES.md) for what this project uses and why,
including the fallback when event data isn't published for a fixture.

> **If you are learning:** start with StatsBomb Open Data. It is free, legal,
> documented, and richer than anything you will scrape. Move to live scraping
> only when you need the current season.

---

## 16. Building the pipeline

The engineering shape of this project, as a template:

```
scheduler → ingest → transform → enrich → quality gate → serve → warehouse
```

| Stage | Job | Lesson |
|---|---|---|
| **Schedule** | One job per fixture, firing after kick-off | Match data isn't ready at full time — allow ~4 h |
| **Ingest** | Pull the raw feed, save it **unmodified** | Always keep the raw payload. Re-parsing beats re-scraping |
| **Transform** | Scale coordinates, compute xT and progression | Where most bugs live |
| **Enrich** | Merge a second source for what the first lacks | No single free source has everything |
| **Quality gate** | Block on structural failure | **Do not skip this** |
| **Serve** | Render visuals | |
| **Load** | Warehouse at event / player-match / team-match grain | Three grains answer three question types |

### Quality checks worth having

Structural, not statistical — these catch scraping and parsing failures:

- Event count in a sane range (800–3000)
- Covers 90+ minutes; both halves present
- Exactly two teams; 11 starters each
- No event references a player outside the squad
- All coordinates within pitch bounds
- **Goal events match the final scoreline** ← catches parsing failures instantly

### Warehouse grains

| Grain | Rows/match | Answers |
|---|---|---|
| `events` | ~1,400 | "Where did this happen?" |
| `match_player_stats` | ~40 | "How did he play?" |
| `match_team_stats` | 2 | "How did they play?" |

Store all three. Deriving player stats from events every time is slow; storing
only aggregates means you can never ask a new spatial question.

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **Big chance** | Analyst-judged "should score" opportunity |
| **Block** | Shot stopped by an outfielder |
| **Carry** | Moving the ball with your feet (not always logged) |
| **Chance created** | Key pass + assists |
| **Expanded minute** | Continuous clock including stoppage |
| **Field tilt** | Share of final-third touches |
| **Final third** | x ≥ 70 on a 105 m pitch |
| **GK sweeper** | Keeper action outside the box |
| **Half-space** | Channel between centre and touchline |
| **High turnover** | Ball won within 40 m of opponent goal |
| **Key pass** | Pass leading directly to a shot |
| **Line-breaking pass** | Pass played between defenders |
| **PPDA** | Passes allowed per defensive action — pressing intensity |
| **Progressive pass** | Pass moving ≥ 9.144 m closer to goal |
| **Set piece** | Corner, free kick, throw-in, penalty |
| **Shot on target** | Would have gone in — excludes blocks |
| **Through ball** | Pass split between defenders into space |
| **xA** | Expected assists — xG of the shot a pass created |
| **xG** | Expected goals — shot quality |
| **xGOT** | Expected goals on target — shot placement |
| **xT** | Expected threat — value added by moving the ball |
| **Zone 14** | Central area outside the box |

---

## 18. Learning path

**Week 1 — Get data in your hands**
Download StatsBomb Open Data. Load one match into pandas. Count event types.
Plot every pass. Do not build anything yet — just look.

**Week 2 — Coordinates and pitches**
Learn `mplsoccer`. Draw a pitch. Plot shots. Build a pass map for one player.
Get the scaling right and prove it with a min/max check.

**Week 3 — Your first real metrics**
Compute possession, field tilt, pass accuracy, progressive passes. Compare your
numbers to a public source for the same match and **investigate every
discrepancy** — that is where the learning is.

**Week 4 — Advanced metrics**
Implement xT from a public grid. Understand why xG ≠ xT ≠ xGOT. Build a shot map
with xG sizing.

**Week 5 — Aggregate and store**
Design a schema at three grains. Load a full season. Write the per-90 view.

**Week 6 — Automate**
Schedule it. Add a quality gate. Make it fail loudly. **This is the step that
separates a notebook from a system** — and the one most portfolios skip.

### Recommended reading

- **Karun Singh — "Introducing Expected Threat"** — the original xT article
- **StatsBomb open data spec** — the best-documented event schema in public
- **mplsoccer documentation** — the pitch-plotting standard
- **Opta event definitions** — the taxonomy most feeds follow

### Practise questions

Answerable with the schema in this repo:

1. Which player has the highest xT per 90 with ≥ 500 minutes?
2. Does the team create more from the left or the right?
3. How does xT per 15 minutes change when leading vs level?
4. Which passing partnership is most load-bearing?
5. Which opponents force the most long balls?

---

*Part of [GunnersAnalysis](../README.md). Every metric here is implemented in
`db/aggregate.py` and `visualizer/match_report.py`; every number is from the
match in `output/2026-2027/mw17_2026-05-24_crystal-palace_v_arsenal/`.*
