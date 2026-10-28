-- =====================================================================
-- Tracking warehouse tables (sibling to event Opta data)
--
-- Apply into the club schema (gunners by default):
--   psql "$GUNNERS_DB_URL" -v ON_ERROR_STOP=1 -c "SET search_path TO gunners, public" \
--     -f db/schema_tracking.sql
-- Or:  make tracking-schema
--
-- Heavy time-series stay on disk as parquet under data/tracking/<match_id>/.
-- These tables hold jobs, aggregates, phases, and optional clip manifests.
-- =====================================================================

CREATE TABLE IF NOT EXISTS tracking_jobs (
    match_id        BIGINT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending',
    video_path      TEXT,
    model           TEXT,
    kickoff_offset_s NUMERIC(10, 3) NOT NULL DEFAULT 0,
    start_s         NUMERIC(10, 3),
    duration_s      NUMERIC(10, 3),
    tracks_path     TEXT,
    meta_path       TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tracking_player_match (
    match_id        BIGINT NOT NULL,
    track_id        INTEGER NOT NULL,
    team            TEXT,
    role            TEXT NOT NULL DEFAULT 'player',
    distance_m      NUMERIC(10, 2),
    sprint_count    INTEGER DEFAULT 0,
    avg_x_m         NUMERIC(8, 3),
    avg_y_m         NUMERIC(8, 3),
    final_third_share NUMERIC(6, 4),
    frames          INTEGER DEFAULT 0,
    PRIMARY KEY (match_id, track_id)
);

CREATE INDEX IF NOT EXISTS idx_tracking_player_match_team
    ON tracking_player_match (match_id, team);

CREATE TABLE IF NOT EXISTS tracking_team_match (
    match_id            BIGINT NOT NULL,
    team                TEXT NOT NULL,
    avg_line_height_m   NUMERIC(8, 3),
    avg_width_m         NUMERIC(8, 3),
    avg_compactness     NUMERIC(12, 3),
    share_def_third     NUMERIC(6, 4),
    share_mid_third     NUMERIC(6, 4),
    share_att_third     NUMERIC(6, 4),
    PRIMARY KEY (match_id, team)
);

CREATE TABLE IF NOT EXISTS match_phases (
    phase_id        BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL,
    team            TEXT,
    phase           TEXT NOT NULL,
    t_start_s       NUMERIC(10, 3) NOT NULL,
    t_end_s         NUMERIC(10, 3) NOT NULL,
    features        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_match_phases_match
    ON match_phases (match_id, t_start_s);

CREATE TABLE IF NOT EXISTS tracking_clip_manifest (
    clip_id         BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL,
    phase_id        BIGINT REFERENCES match_phases(phase_id) ON DELETE SET NULL,
    path            TEXT NOT NULL,
    t_start_s       NUMERIC(10, 3) NOT NULL,
    t_end_s         NUMERIC(10, 3) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tracking_clips_match
    ON tracking_clip_manifest (match_id);
