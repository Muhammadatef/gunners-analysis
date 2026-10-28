-- =====================================================================
-- GunnersAnalysis - season warehouse schema (PostgreSQL 14+)
--
-- Grain summary
--   fixtures            1 row per scheduled Arsenal match (season calendar)
--   matches             1 row per played match that has been ingested
--   match_team_stats    2 rows per match (one per team)
--   match_player_stats  ~30 rows per match (one per player who appeared)
--   events              ~1400 rows per match (the raw Opta event stream)
--   shots               1 row per shot (denormalised view of events, for xG work)
--   pass_links          1 row per ordered passer->receiver pair per match
--   visualizations      1 row per rendered PNG
--   pipeline_runs       1 row per DAG/pipeline execution
--   quality_reports     1 row per validation run
--
-- Everything keys off whoscored/Opta ids so re-ingesting a match is idempotent.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS gunners;
SET search_path TO gunners, public;

-- ---------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS seasons (
    season_id       SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,          -- '2026/2027'
    competition     TEXT NOT NULL DEFAULT 'Premier League',
    start_date      DATE,
    end_date        DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS teams (
    team_id         INTEGER PRIMARY KEY,           -- WhoScored/Opta team id
    name            TEXT NOT NULL,
    short_name      TEXT,
    slug            TEXT,                          -- whoscored url slug
    primary_color   TEXT,                          -- hex, used by the visualiser
    logo_path       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS players (
    player_id       INTEGER PRIMARY KEY,           -- WhoScored/Opta player id
    name            TEXT NOT NULL,
    short_name      TEXT,
    height_cm       INTEGER,
    weight_kg       INTEGER,
    date_of_birth   DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A player's club membership over time (handles transfers mid-season).
CREATE TABLE IF NOT EXISTS player_team_spells (
    spell_id        BIGSERIAL PRIMARY KEY,
    player_id       INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    season_id       INTEGER REFERENCES seasons(season_id) ON DELETE SET NULL,
    first_seen      DATE,
    last_seen       DATE,
    UNIQUE (player_id, team_id, season_id)
);

-- ---------------------------------------------------------------------
-- Season calendar: what we intend to scrape
-- ---------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE fixture_status AS ENUM (
        'scheduled',    -- in the calendar, not played yet
        'resolving',    -- looking up the whoscored match id
        'ready',        -- match id resolved, waiting for full time
        'ingesting',    -- pipeline running
        'ingested',     -- data + visuals landed
        'failed',       -- pipeline gave up
        'postponed'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id      SERIAL PRIMARY KEY,
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
    fixture_key     TEXT NOT NULL UNIQUE,          -- 'gw17_2026-12-26_crystal-palace_v_arsenal'
    matchweek       SMALLINT NOT NULL,
    scheduled_date  DATE NOT NULL,
    kickoff_uk      TIME,                          -- null until the TV slot is confirmed
    kickoff_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    home_team_name  TEXT NOT NULL,
    away_team_name  TEXT NOT NULL,
    home_team_id    INTEGER REFERENCES teams(team_id),
    away_team_id    INTEGER REFERENCES teams(team_id),
    opponent        TEXT NOT NULL,                 -- always the non-Arsenal side
    venue           CHAR(1) NOT NULL CHECK (venue IN ('H', 'A')),
    whoscored_slug  TEXT NOT NULL,
    whoscored_match_id BIGINT,                     -- resolved a few weeks before kick-off
    match_url       TEXT,
    status          fixture_status NOT NULL DEFAULT 'scheduled',
    attempts        SMALLINT NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fixtures_date ON fixtures (scheduled_date);
CREATE INDEX IF NOT EXISTS idx_fixtures_status ON fixtures (status);
CREATE INDEX IF NOT EXISTS idx_fixtures_season_mw ON fixtures (season_id, matchweek);

-- ---------------------------------------------------------------------
-- Match facts
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS matches (
    match_id        BIGINT PRIMARY KEY,            -- WhoScored match id
    fixture_id      INTEGER REFERENCES fixtures(fixture_id) ON DELETE SET NULL,
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id) ON DELETE CASCADE,
    competition     TEXT NOT NULL DEFAULT 'Premier League',
    matchweek       SMALLINT,
    kickoff_utc     TIMESTAMPTZ,
    match_date      DATE NOT NULL,
    home_team_id    INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id    INTEGER NOT NULL REFERENCES teams(team_id),
    home_score      SMALLINT NOT NULL,
    away_score      SMALLINT NOT NULL,
    ht_home_score   SMALLINT,
    ht_away_score   SMALLINT,
    -- xG/xGOT are not in the WhoScored feed; sourced manually (Fotmob) or by a model
    home_xg         NUMERIC(5,2),
    away_xg         NUMERIC(5,2),
    home_xgot       NUMERIC(5,2),
    away_xgot       NUMERIC(5,2),
    xg_source       TEXT,                          -- 'fotmob' | 'manual' | 'model' | NULL
    venue_name      TEXT,
    referee         TEXT,
    attendance      INTEGER,
    max_minute      SMALLINT,
    event_count     INTEGER,
    job_id          UUID,                          -- pipeline run that produced this
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_match_teams_differ CHECK (home_team_id <> away_team_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_date ON matches (match_date);
CREATE INDEX IF NOT EXISTS idx_matches_season ON matches (season_id, matchweek);
CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches (home_team_id, away_team_id);

-- Per-team, per-match aggregates. These are exactly the numbers the
-- "Match Stats" and "Shot Map" panels render, so the dashboard and the
-- warehouse can never disagree.
CREATE TABLE IF NOT EXISTS match_team_stats (
    match_id            BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    team_id             INTEGER NOT NULL REFERENCES teams(team_id),
    is_home             BOOLEAN NOT NULL,
    opponent_team_id    INTEGER NOT NULL REFERENCES teams(team_id),
    goals               SMALLINT NOT NULL DEFAULT 0,
    goals_conceded      SMALLINT NOT NULL DEFAULT 0,
    xg                  NUMERIC(5,2),
    xgot                NUMERIC(5,2),
    xt                  NUMERIC(6,3),              -- summed threat added by passes
    possession_pct      NUMERIC(5,2),
    field_tilt_pct      NUMERIC(5,2),
    passes              INTEGER,
    passes_accurate     INTEGER,
    long_balls          INTEGER,
    long_balls_accurate INTEGER,
    crosses             INTEGER,
    crosses_accurate    INTEGER,
    corners             INTEGER,
    throw_ins           INTEGER,
    goalkick_avg_length NUMERIC(5,2),
    progressive_passes  INTEGER,
    key_passes          INTEGER,
    zone14_passes       INTEGER,
    halfspace_passes    INTEGER,
    take_ons            INTEGER,
    take_ons_won        INTEGER,
    shots               INTEGER,
    shots_on_target     INTEGER,
    big_chances         INTEGER,
    avg_shot_distance   NUMERIC(5,2),
    tackles             INTEGER,
    tackles_won         INTEGER,
    interceptions       INTEGER,
    clearances          INTEGER,
    aerials             INTEGER,
    aerials_won         INTEGER,
    ball_recoveries     INTEGER,
    blocked_passes      INTEGER,
    fouls               INTEGER,
    offsides            INTEGER,
    high_turnovers      INTEGER,
    def_action_height_m NUMERIC(5,2),              -- avg x of defensive actions
    pass_line_height_m  NUMERIC(5,2),              -- median x of the pass network
    PRIMARY KEY (match_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_mts_team ON match_team_stats (team_id);

CREATE TABLE IF NOT EXISTS match_player_stats (
    match_id            BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    player_id           INTEGER NOT NULL REFERENCES players(player_id),
    team_id             INTEGER NOT NULL REFERENCES teams(team_id),
    shirt_no            SMALLINT,
    position            TEXT,
    is_first_eleven     BOOLEAN,
    is_motm             BOOLEAN DEFAULT FALSE,
    minutes_played      SMALLINT,
    subbed_on_minute    SMALLINT,
    subbed_off_minute   SMALLINT,
    rating              NUMERIC(4,2),
    touches             INTEGER,
    goals               SMALLINT DEFAULT 0,
    own_goals           SMALLINT DEFAULT 0,
    assists             SMALLINT DEFAULT 0,
    shots               INTEGER DEFAULT 0,
    shots_on_target     INTEGER DEFAULT 0,
    key_passes          INTEGER DEFAULT 0,
    passes              INTEGER DEFAULT 0,
    passes_accurate     INTEGER DEFAULT 0,
    progressive_passes  INTEGER DEFAULT 0,
    through_balls       INTEGER DEFAULT 0,
    crosses             INTEGER DEFAULT 0,
    xt                  NUMERIC(6,3),
    take_ons            INTEGER DEFAULT 0,
    take_ons_won        INTEGER DEFAULT 0,
    tackles             INTEGER DEFAULT 0,
    tackles_won         INTEGER DEFAULT 0,
    interceptions       INTEGER DEFAULT 0,
    clearances          INTEGER DEFAULT 0,
    blocks              INTEGER DEFAULT 0,
    aerials             INTEGER DEFAULT 0,
    aerials_won         INTEGER DEFAULT 0,
    ball_recoveries     INTEGER DEFAULT 0,
    dispossessed        INTEGER DEFAULT 0,
    fouls               INTEGER DEFAULT 0,
    fouled              INTEGER DEFAULT 0,
    yellow_cards        SMALLINT DEFAULT 0,
    red_cards           SMALLINT DEFAULT 0,
    saves               INTEGER DEFAULT 0,          -- goalkeepers
    goals_conceded      INTEGER DEFAULT 0,
    avg_x               NUMERIC(5,2),               -- median touch position (105x68)
    avg_y               NUMERIC(5,2),
    PRIMARY KEY (match_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_mps_player ON match_player_stats (player_id);
CREATE INDEX IF NOT EXISTS idx_mps_team ON match_player_stats (team_id);

-- ---------------------------------------------------------------------
-- Event stream - the grain everything else is derived from
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    event_uid       BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    opta_event_id   BIGINT,                        -- 'id' in the feed
    event_index     INTEGER NOT NULL,              -- order within the match
    period          TEXT,
    minute          SMALLINT,
    second          SMALLINT,
    expanded_minute SMALLINT,
    team_id         INTEGER REFERENCES teams(team_id),
    player_id       INTEGER REFERENCES players(player_id),
    type            TEXT NOT NULL,
    outcome         TEXT,
    -- coordinates normalised to a 105x68 pitch
    x               NUMERIC(6,2),
    y               NUMERIC(6,2),
    end_x           NUMERIC(6,2),
    end_y           NUMERIC(6,2),
    goal_mouth_y    NUMERIC(6,2),
    goal_mouth_z    NUMERIC(6,2),
    is_touch        BOOLEAN,
    is_shot         BOOLEAN,
    is_goal         BOOLEAN,
    card_type       TEXT,
    -- derived
    xt_start        NUMERIC(8,5),
    xt_end          NUMERIC(8,5),
    xt_delta        NUMERIC(8,5),
    progression_m   NUMERIC(6,2),                  -- metres closer to goal (passes)
    is_progressive  BOOLEAN,                       -- progression_m >= 9.144
    qualifiers      JSONB,
    UNIQUE (match_id, event_index)
);

CREATE INDEX IF NOT EXISTS idx_events_match ON events (match_id);
CREATE INDEX IF NOT EXISTS idx_events_player ON events (player_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (match_id, type);
CREATE INDEX IF NOT EXISTS idx_events_qualifiers ON events USING GIN (qualifiers);

CREATE TABLE IF NOT EXISTS shots (
    shot_id         BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    event_uid       BIGINT REFERENCES events(event_uid) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_id       INTEGER REFERENCES players(player_id),
    minute          SMALLINT,
    outcome         TEXT NOT NULL,                 -- Goal|SavedShot|MissedShots|ShotOnPost
    is_goal         BOOLEAN NOT NULL DEFAULT FALSE,
    is_own_goal     BOOLEAN NOT NULL DEFAULT FALSE,
    is_big_chance   BOOLEAN NOT NULL DEFAULT FALSE,
    is_penalty      BOOLEAN NOT NULL DEFAULT FALSE,
    is_header       BOOLEAN NOT NULL DEFAULT FALSE,
    body_part       TEXT,
    situation       TEXT,                          -- RegularPlay|FromCorner|SetPiece|...
    x               NUMERIC(6,2),
    y               NUMERIC(6,2),
    distance_m      NUMERIC(6,2),
    goal_mouth_y    NUMERIC(6,2),
    goal_mouth_z    NUMERIC(6,2),
    assisted_by     INTEGER REFERENCES players(player_id),
    xg              NUMERIC(5,3),                  -- filled if/when a model runs
    UNIQUE (match_id, event_uid)
);

CREATE INDEX IF NOT EXISTS idx_shots_match ON shots (match_id);
CREATE INDEX IF NOT EXISTS idx_shots_player ON shots (player_id);

-- Passer -> receiver volume, i.e. the edges of the pass network panel.
CREATE TABLE IF NOT EXISTS pass_links (
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    passer_id       INTEGER NOT NULL REFERENCES players(player_id),
    receiver_id     INTEGER NOT NULL REFERENCES players(player_id),
    pass_count      INTEGER NOT NULL,
    PRIMARY KEY (match_id, passer_id, receiver_id)
);

CREATE INDEX IF NOT EXISTS idx_pass_links_team ON pass_links (match_id, team_id);

-- ---------------------------------------------------------------------
-- Pipeline bookkeeping
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pipeline_runs (
    job_id          UUID PRIMARY KEY,
    fixture_id      INTEGER REFERENCES fixtures(fixture_id) ON DELETE SET NULL,
    match_id        BIGINT REFERENCES matches(match_id) ON DELETE SET NULL,
    dag_id          TEXT,
    dag_run_id      TEXT,
    stage           TEXT,                          -- scrape|transform|validate|visualize|load
    status          TEXT NOT NULL,                 -- running|success|failed
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    duration_s      NUMERIC(10,2),
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_fixture ON pipeline_runs (fixture_id);

CREATE TABLE IF NOT EXISTS quality_reports (
    report_id       BIGSERIAL PRIMARY KEY,
    job_id          UUID REFERENCES pipeline_runs(job_id) ON DELETE CASCADE,
    match_id        BIGINT REFERENCES matches(match_id) ON DELETE CASCADE,
    passed          BOOLEAN NOT NULL,
    checks_total    SMALLINT,
    checks_failed   SMALLINT,
    report          JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS visualizations (
    viz_id          BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    job_id          UUID REFERENCES pipeline_runs(job_id) ON DELETE SET NULL,
    panel           TEXT NOT NULL,                 -- '01_home_pass_network' ... '00_..._unified'
    title           TEXT,
    file_path       TEXT NOT NULL,
    width_px        INTEGER,
    height_px       INTEGER,
    bytes           BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, panel)
);

-- ---------------------------------------------------------------------
-- Season tracking views
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW v_team_match_results AS
SELECT
    m.match_id,
    m.season_id,
    m.matchweek,
    m.match_date,
    s.team_id,
    s.opponent_team_id,
    s.is_home,
    s.goals,
    s.goals_conceded,
    (s.goals - s.goals_conceded)                        AS goal_difference,
    CASE WHEN s.goals > s.goals_conceded THEN 'W'
         WHEN s.goals = s.goals_conceded THEN 'D'
         ELSE 'L' END                                   AS result,
    CASE WHEN s.goals > s.goals_conceded THEN 3
         WHEN s.goals = s.goals_conceded THEN 1
         ELSE 0 END                                     AS points,
    s.xg, s.xgot, s.xt, s.possession_pct, s.field_tilt_pct,
    s.shots, s.shots_on_target, s.progressive_passes
FROM match_team_stats s
JOIN matches m USING (match_id);

-- Running season table for any club we have ingested.
CREATE OR REPLACE VIEW v_team_season_summary AS
SELECT
    r.season_id,
    r.team_id,
    t.name                                       AS team_name,
    COUNT(*)                                     AS played,
    SUM((r.result = 'W')::int)                   AS won,
    SUM((r.result = 'D')::int)                   AS drawn,
    SUM((r.result = 'L')::int)                   AS lost,
    SUM(r.goals)                                 AS goals_for,
    SUM(r.goals_conceded)                        AS goals_against,
    SUM(r.goal_difference)                       AS goal_difference,
    SUM(r.points)                                AS points,
    ROUND(AVG(r.possession_pct), 2)              AS avg_possession,
    ROUND(AVG(r.field_tilt_pct), 2)              AS avg_field_tilt,
    ROUND(SUM(r.xg), 2)                          AS total_xg,
    ROUND(SUM(r.xt), 2)                          AS total_xt,
    SUM(r.shots)                                 AS total_shots,
    SUM(r.shots_on_target)                       AS total_shots_on_target
FROM v_team_match_results r
JOIN teams t ON t.team_id = r.team_id
GROUP BY r.season_id, r.team_id, t.name;

-- Player season totals + per-90s, the thing you actually want to sort on.
CREATE OR REPLACE VIEW v_player_season_totals AS
SELECT
    m.season_id,
    p.player_id,
    p.name                                       AS player_name,
    ps.team_id,
    t.name                                       AS team_name,
    COUNT(*)                                     AS appearances,
    SUM((ps.is_first_eleven)::int)               AS starts,
    SUM(ps.minutes_played)                       AS minutes,
    SUM(ps.goals)                                AS goals,
    SUM(ps.assists)                              AS assists,
    SUM(ps.shots)                                AS shots,
    SUM(ps.key_passes)                           AS key_passes,
    SUM(ps.progressive_passes)                   AS progressive_passes,
    SUM(ps.passes)                               AS passes,
    SUM(ps.passes_accurate)                      AS passes_accurate,
    SUM(ps.take_ons_won)                         AS take_ons_won,
    SUM(ps.tackles)                              AS tackles,
    SUM(ps.interceptions)                        AS interceptions,
    SUM(ps.ball_recoveries)                      AS ball_recoveries,
    SUM(ps.aerials_won)                          AS aerials_won,
    ROUND(SUM(ps.xt), 3)                         AS total_xt,
    ROUND(AVG(ps.rating), 2)                     AS avg_rating,
    ROUND(SUM(ps.goals) * 90.0
          / NULLIF(SUM(ps.minutes_played), 0), 2) AS goals_per_90,
    ROUND(SUM(ps.assists) * 90.0
          / NULLIF(SUM(ps.minutes_played), 0), 2) AS assists_per_90,
    ROUND(SUM(ps.progressive_passes) * 90.0
          / NULLIF(SUM(ps.minutes_played), 0), 2) AS prog_passes_per_90,
    ROUND(SUM(ps.xt) * 90.0
          / NULLIF(SUM(ps.minutes_played), 0), 3) AS xt_per_90
FROM match_player_stats ps
JOIN matches m USING (match_id)
JOIN players p USING (player_id)
JOIN teams   t ON t.team_id = ps.team_id
GROUP BY m.season_id, p.player_id, p.name, ps.team_id, t.name;

-- What still needs to run, in kick-off order.
CREATE OR REPLACE VIEW v_season_progress AS
SELECT
    f.matchweek,
    f.scheduled_date,
    f.kickoff_uk,
    f.venue,
    f.opponent,
    f.status,
    f.whoscored_match_id,
    m.home_score,
    m.away_score,
    (SELECT COUNT(*) FROM visualizations v WHERE v.match_id = m.match_id) AS panels_rendered
FROM fixtures f
LEFT JOIN matches m ON m.fixture_id = f.fixture_id
ORDER BY f.scheduled_date, f.matchweek;

-- ---------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fixtures_touch ON fixtures;
CREATE TRIGGER trg_fixtures_touch BEFORE UPDATE ON fixtures
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_teams_touch ON teams;
CREATE TRIGGER trg_teams_touch BEFORE UPDATE ON teams
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_players_touch ON players;
CREATE TRIGGER trg_players_touch BEFORE UPDATE ON players
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
