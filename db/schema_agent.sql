-- =====================================================================
-- GunnersAnalysis - analyst layer schema
--
-- Everything here is *derived* from the warehouse in db/schema.sql. Nothing in
-- this file is a source of truth: drop every table and a rebuild reproduces it
-- byte for byte from `events`. That is deliberate - the master prompt's rule is
-- that Postgres holds the facts and the analyst layer holds interpretations of
-- them, and an interpretation that cannot be regenerated is a liability.
--
--   possession_sequences     1 row per possession (~250 per match)
--   pressure_events          1 row per defensive engagement in open play
--   match_tactical_features  1 row per (match, team) - PPDA and friends
--   semantic_documents       1 row per natural-language document + embedding
--
-- Kept separate from schema.sql so the warehouse DDL stays untouched. Applied
-- by db/apply_agent_schema.py, and mounted after schema.sql on a fresh volume.
-- =====================================================================

-- pgvector lives in `public` so every club schema shares one copy of the
-- extension rather than installing it per schema.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

SET search_path TO gunners, public;

-- ---------------------------------------------------------------------
-- possession sequences
-- ---------------------------------------------------------------------
-- Opta emits no possession id, so sequences are segmented from the event
-- stream: a sequence belongs to one team and ends only when the opposition win
-- *controlled* possession. Their defensive contacts inside the attack - a
-- block, a challenge, a deflection off a defender - do not end it. Getting
-- that wrong collapses most sequences to a single event.

CREATE TABLE IF NOT EXISTS possession_sequences (
    sequence_id     BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    sequence_index  INTEGER NOT NULL,              -- order within the match

    start_event_uid BIGINT REFERENCES events(event_uid) ON DELETE SET NULL,
    end_event_uid   BIGINT REFERENCES events(event_uid) ON DELETE SET NULL,
    period          TEXT,
    start_minute    SMALLINT,
    start_second    SMALLINT,
    end_minute      SMALLINT,
    end_second      SMALLINT,
    duration_s      NUMERIC(6,1),

    events          INTEGER NOT NULL,
    passes          INTEGER NOT NULL DEFAULT 0,
    players         INTEGER NOT NULL DEFAULT 0,

    start_x         NUMERIC(6,2),                  -- where the team won it
    start_y         NUMERIC(6,2),
    end_x           NUMERIC(6,2),
    end_y           NUMERIC(6,2),
    max_x           NUMERIC(6,2),                  -- furthest upfield reached
    progression_m   NUMERIC(7,2),                  -- start_x -> max_x, in metres
    xt_sum          NUMERIC(8,5),

    -- how it started and how it finished, which is what makes a sequence
    -- describable in words
    start_type      TEXT,                          -- the event type that began it
    build_zone      TEXT,                          -- defensive | middle | attacking
    is_high_turnover BOOLEAN DEFAULT FALSE,        -- won in the attacking third
    end_type        TEXT,                          -- how possession was lost/ended
    ended_in_shot   BOOLEAN DEFAULT FALSE,
    ended_in_goal   BOOLEAN DEFAULT FALSE,

    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (match_id, sequence_index)
);

CREATE INDEX IF NOT EXISTS idx_seq_match ON possession_sequences (match_id);
CREATE INDEX IF NOT EXISTS idx_seq_team ON possession_sequences (match_id, team_id);
CREATE INDEX IF NOT EXISTS idx_seq_shot ON possession_sequences (match_id, ended_in_shot);

-- ---------------------------------------------------------------------
-- pressure events
-- ---------------------------------------------------------------------
-- Opta has no pressure event either. What it does have is defensive actions,
-- and PPDA is defined over exactly those, so this table records each defensive
-- engagement with the pitch context needed to aggregate it: how far upfield,
-- in which third, and how quickly after the opponent gained the ball.

CREATE TABLE IF NOT EXISTS pressure_events (
    pressure_id     BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    event_uid       BIGINT REFERENCES events(event_uid) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_id       INTEGER REFERENCES players(player_id),
    opponent_team_id INTEGER REFERENCES teams(team_id),

    minute          SMALLINT,
    second          SMALLINT,
    period          TEXT,
    type            TEXT NOT NULL,                 -- Tackle | Interception | ...
    outcome         TEXT,
    x               NUMERIC(6,2),
    y               NUMERIC(6,2),
    third           TEXT,                          -- defensive | middle | attacking
    is_counterpress BOOLEAN DEFAULT FALSE,         -- within 5s of losing the ball
    seconds_since_loss NUMERIC(6,1),

    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (match_id, event_uid)
);

CREATE INDEX IF NOT EXISTS idx_pressure_match ON pressure_events (match_id);
CREATE INDEX IF NOT EXISTS idx_pressure_team ON pressure_events (match_id, team_id);

-- ---------------------------------------------------------------------
-- carries
-- ---------------------------------------------------------------------
-- The ball moving at a player's feet, which Opta never records: it gives the
-- pass that arrives and the pass that leaves, and nothing about the twenty
-- metres driven in between. Inferred from consecutive touches, and kept in its
-- own table rather than mixed into `events` so an inference is never mistaken
-- for an observation.

CREATE TABLE IF NOT EXISTS carries (
    carry_id        BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    event_uid       BIGINT REFERENCES events(event_uid) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_id       INTEGER REFERENCES players(player_id),

    period          TEXT,
    minute          SMALLINT,
    second          SMALLINT,
    start_x         NUMERIC(6,2),
    start_y         NUMERIC(6,2),
    end_x           NUMERIC(6,2),
    end_y           NUMERIC(6,2),
    distance_m      NUMERIC(6,2),
    progression_m   NUMERIC(6,2),                  -- forward metres only
    duration_s      NUMERIC(6,1),
    start_zone      TEXT,
    end_zone        TEXT,
    is_progressive  BOOLEAN DEFAULT FALSE,         -- >= 9.144 m upfield
    ends_in_shot    BOOLEAN DEFAULT FALSE,

    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (match_id, event_uid)
);

CREATE INDEX IF NOT EXISTS idx_carries_match ON carries (match_id);
CREATE INDEX IF NOT EXISTS idx_carries_player ON carries (player_id);

-- ---------------------------------------------------------------------
-- match tactical features
-- ---------------------------------------------------------------------
-- Two rows per match, the tactical counterpart to match_team_stats. Kept
-- separate rather than widening that table: match_team_stats mirrors the
-- rendered panels and changing it would change what the dashboards assert.

CREATE TABLE IF NOT EXISTS match_tactical_features (
    match_id        BIGINT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    opponent_team_id INTEGER REFERENCES teams(team_id),
    is_home         BOOLEAN,

    -- pressing
    ppda            NUMERIC(7,2),                  -- opp passes / our def actions, opp half
    ppda_allowed    NUMERIC(7,2),                  -- the same measured against us
    def_actions_opp_half INTEGER,
    counterpresses  INTEGER,
    high_turnovers  INTEGER,

    -- possession shape
    sequences       INTEGER,
    avg_sequence_events NUMERIC(6,2),
    avg_sequence_duration_s NUMERIC(6,2),
    direct_speed_ms NUMERIC(6,3),                  -- upfield metres per second
    long_sequences  INTEGER,                       -- >= 10 events
    sequences_ending_in_shot INTEGER,

    -- territory
    field_tilt_pct  NUMERIC(5,2),
    avg_start_x     NUMERIC(6,2),
    build_from_defensive_third INTEGER,
    build_from_high_turnover INTEGER,

    created_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (match_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_tactical_team ON match_tactical_features (team_id);

-- ---------------------------------------------------------------------
-- semantic documents
-- ---------------------------------------------------------------------
-- Natural-language descriptions of what happened, generated from the tables
-- above and embedded for similarity search. The master prompt's rule: do not
-- embed raw tables. A row of numbers retrieves badly; a sentence describing
-- what those numbers mean retrieves well.
--
-- These are a derived artefact and never a second source of truth. Every
-- number an answer quotes is fetched from SQL at answer time -- retrieval
-- supplies tactical *context*, not facts. That division is what makes the
-- anti-hallucination guarantee mechanical rather than aspirational.
--
-- The embedding lives beside the events that generated it, so metadata
-- pre-filtering (this opponent, this phase, this score state) and vector
-- search happen in a single query.

CREATE TABLE IF NOT EXISTS semantic_documents (
    document_id     BIGSERIAL PRIMARY KEY,
    doc_type        TEXT NOT NULL,        -- match | sequence | shot | player | tactical | pressing
    source_table    TEXT,                 -- what it was generated from
    source_id       BIGINT,               -- the row it describes

    content         TEXT NOT NULL,
    -- BAAI/bge-small-en-v1.5 via fastembed: 384 dimensions, ONNX, no torch.
    -- OpenRouter serves chat only, so embeddings are computed locally.
    embedding       vector(384),
    embedding_model TEXT,

    -- metadata for pre-filtering, per the spec
    match_id        BIGINT REFERENCES matches(match_id) ON DELETE CASCADE,
    season_id       INTEGER REFERENCES seasons(season_id) ON DELETE CASCADE,
    team_id         INTEGER REFERENCES teams(team_id),
    opponent        TEXT,
    match_date      DATE,
    venue           CHAR(1),
    phase           TEXT,                 -- build-up | attacking | defensive | transition | set-piece
    period          TEXT,
    player_ids      INTEGER[],
    score_state     TEXT,                 -- leading | level | trailing at the time
    minute          SMALLINT,

    token_estimate  INTEGER,
    built_at        TIMESTAMPTZ DEFAULT now(),
    UNIQUE (doc_type, source_table, source_id)
);

CREATE INDEX IF NOT EXISTS idx_docs_match ON semantic_documents (match_id);
CREATE INDEX IF NOT EXISTS idx_docs_type ON semantic_documents (doc_type);
CREATE INDEX IF NOT EXISTS idx_docs_opponent ON semantic_documents (opponent);
CREATE INDEX IF NOT EXISTS idx_docs_players ON semantic_documents USING GIN (player_ids);

-- The lexical half of hybrid retrieval. bge-small is a paraphrase model and
-- undershoots tokens that carry no semantics of their own — "PPDA", "Zone 14",
-- a surname — which is most of what a football question is made of, so the
-- retriever fuses a full-text ranking with the vector one. Generated and
-- STORED rather than computed per query: the corpus is written once per match
-- and read on every question, and only a stored column can carry an index.
--
-- ALTER rather than a column in the CREATE above, because CREATE TABLE IF NOT
-- EXISTS is a no-op on the databases that already hold the 2025/26 corpus.
ALTER TABLE semantic_documents
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_docs_content_tsv ON semantic_documents
    USING GIN (content_tsv);

-- HNSW rather than IVFFlat: the corpus is a few thousand rows and static
-- between matches, so build cost is irrelevant and recall is what matters.
-- IVFFlat also needs training data present before the index is meaningful,
-- which is an awkward ordering constraint for a table built incrementally.
CREATE INDEX IF NOT EXISTS idx_docs_embedding ON semantic_documents
    USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------
-- chat conversations (web /analyst service)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS conversations (
  id           UUID PRIMARY KEY,
  club         TEXT NOT NULL,
  title        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id                  UUID PRIMARY KEY,
  conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role                TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content             TEXT NOT NULL,
  tool                TEXT,
  query_id            TEXT,
  parameters          JSONB,
  evidence            JSONB,
  evidence_numbers    NUMERIC[],
  unsupported_numbers NUMERIC[],
  retrieved           JSONB,
  reasoning           TEXT,
  latency_s           NUMERIC,
  cost_usd            NUMERIC,
  mlflow_run_id       TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages (conversation_id, created_at);
