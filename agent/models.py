"""SQLAlchemy models for the analyst layer.

The warehouse itself is raw psycopg2 and stays that way — `db/loader.py` is the
one writer and it is not worth rewriting. These models cover the derived tables
and the document store, where an ORM genuinely helps: the retriever composes
filters dynamically, and building that as string SQL is how injection bugs and
unreadable code get written.

**The search_path trap.** Nothing in this codebase is schema-qualified. One club
is one Postgres schema, and `db/connection.py` selects it by issuing
`SET search_path` on each connection. A SQLAlchemy engine pools connections, so
a session that does not set the path itself will read whichever schema the
pooled connection happened to be left on — in practice `gunners`, silently, for
every club. `engine_for()` therefore attaches the path to the connection at
checkout rather than trusting ambient state.

Models are declared without a schema for exactly the same reason: the schema is
chosen at connection time, not baked into the class.
"""

from __future__ import annotations

import os
import sys

ROOT = os.environ.get('GUNNERS_ROOT',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pgvector.sqlalchemy import Vector                                # noqa: E402
from sqlalchemy import (ARRAY, BigInteger, Boolean, CHAR, Date, DateTime,  # noqa: E402
                        ForeignKey, Integer, Numeric, SmallInteger, String,
                        Text, create_engine, event, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker  # noqa: E402

from db.connection import DEFAULT_SCHEMA, club_schema, dsn  # noqa: E402

EMBEDDING_DIM = 384          # BAAI/bge-small-en-v1.5


class Base(DeclarativeBase):
    pass


class PossessionSequence(Base):
    __tablename__ = 'possession_sequences'

    sequence_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(BigInteger, index=True)
    team_id: Mapped[int] = mapped_column(Integer)
    sequence_index: Mapped[int] = mapped_column(Integer)

    start_event_uid: Mapped[int | None] = mapped_column(BigInteger)
    end_event_uid: Mapped[int | None] = mapped_column(BigInteger)
    period: Mapped[str | None] = mapped_column(Text)
    start_minute: Mapped[int | None] = mapped_column(SmallInteger)
    start_second: Mapped[int | None] = mapped_column(SmallInteger)
    end_minute: Mapped[int | None] = mapped_column(SmallInteger)
    end_second: Mapped[int | None] = mapped_column(SmallInteger)
    duration_s: Mapped[float | None] = mapped_column(Numeric(6, 1))

    events: Mapped[int] = mapped_column(Integer)
    passes: Mapped[int] = mapped_column(Integer)
    players: Mapped[int] = mapped_column(Integer)

    start_x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    start_y: Mapped[float | None] = mapped_column(Numeric(6, 2))
    end_x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    end_y: Mapped[float | None] = mapped_column(Numeric(6, 2))
    max_x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    progression_m: Mapped[float | None] = mapped_column(Numeric(7, 2))
    xt_sum: Mapped[float | None] = mapped_column(Numeric(8, 5))

    start_type: Mapped[str | None] = mapped_column(Text)
    build_zone: Mapped[str | None] = mapped_column(Text)
    is_high_turnover: Mapped[bool | None] = mapped_column(Boolean)
    end_type: Mapped[str | None] = mapped_column(Text)
    ended_in_shot: Mapped[bool | None] = mapped_column(Boolean)
    ended_in_goal: Mapped[bool | None] = mapped_column(Boolean)


class PressureEvent(Base):
    __tablename__ = 'pressure_events'

    pressure_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_uid: Mapped[int | None] = mapped_column(BigInteger)
    team_id: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[int | None] = mapped_column(Integer)
    opponent_team_id: Mapped[int | None] = mapped_column(Integer)

    minute: Mapped[int | None] = mapped_column(SmallInteger)
    second: Mapped[int | None] = mapped_column(SmallInteger)
    period: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    y: Mapped[float | None] = mapped_column(Numeric(6, 2))
    third: Mapped[str | None] = mapped_column(Text)
    is_counterpress: Mapped[bool | None] = mapped_column(Boolean)
    seconds_since_loss: Mapped[float | None] = mapped_column(Numeric(6, 1))


class Carry(Base):
    __tablename__ = 'carries'

    carry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_uid: Mapped[int | None] = mapped_column(BigInteger)
    team_id: Mapped[int] = mapped_column(Integer)
    player_id: Mapped[int | None] = mapped_column(Integer)

    period: Mapped[str | None] = mapped_column(Text)
    minute: Mapped[int | None] = mapped_column(SmallInteger)
    second: Mapped[int | None] = mapped_column(SmallInteger)
    start_x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    start_y: Mapped[float | None] = mapped_column(Numeric(6, 2))
    end_x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    end_y: Mapped[float | None] = mapped_column(Numeric(6, 2))
    distance_m: Mapped[float | None] = mapped_column(Numeric(6, 2))
    progression_m: Mapped[float | None] = mapped_column(Numeric(6, 2))
    duration_s: Mapped[float | None] = mapped_column(Numeric(6, 1))
    start_zone: Mapped[str | None] = mapped_column(Text)
    end_zone: Mapped[str | None] = mapped_column(Text)
    is_progressive: Mapped[bool | None] = mapped_column(Boolean)
    ends_in_shot: Mapped[bool | None] = mapped_column(Boolean)


class MatchTacticalFeatures(Base):
    __tablename__ = 'match_tactical_features'

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opponent_team_id: Mapped[int | None] = mapped_column(Integer)
    is_home: Mapped[bool | None] = mapped_column(Boolean)

    ppda: Mapped[float | None] = mapped_column(Numeric(7, 2))
    ppda_allowed: Mapped[float | None] = mapped_column(Numeric(7, 2))
    def_actions_opp_half: Mapped[int | None] = mapped_column(Integer)
    counterpresses: Mapped[int | None] = mapped_column(Integer)
    high_turnovers: Mapped[int | None] = mapped_column(Integer)

    sequences: Mapped[int | None] = mapped_column(Integer)
    avg_sequence_events: Mapped[float | None] = mapped_column(Numeric(6, 2))
    avg_sequence_duration_s: Mapped[float | None] = mapped_column(Numeric(6, 2))
    direct_speed_ms: Mapped[float | None] = mapped_column(Numeric(6, 3))
    long_sequences: Mapped[int | None] = mapped_column(Integer)
    sequences_ending_in_shot: Mapped[int | None] = mapped_column(Integer)

    field_tilt_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    avg_start_x: Mapped[float | None] = mapped_column(Numeric(6, 2))
    build_from_defensive_third: Mapped[int | None] = mapped_column(Integer)
    build_from_high_turnover: Mapped[int | None] = mapped_column(Integer)


class SemanticDocument(Base):
    """A natural-language description of something that happened, plus its
    embedding and the metadata needed to pre-filter before searching."""

    __tablename__ = 'semantic_documents'

    document_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    doc_type: Mapped[str] = mapped_column(Text, index=True)
    source_table: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(BigInteger)

    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(Text)

    match_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    season_id: Mapped[int | None] = mapped_column(Integer)
    team_id: Mapped[int | None] = mapped_column(Integer)
    opponent: Mapped[str | None] = mapped_column(Text, index=True)
    match_date: Mapped[object | None] = mapped_column(Date)
    venue: Mapped[str | None] = mapped_column(CHAR(1))
    phase: Mapped[str | None] = mapped_column(Text)
    period: Mapped[str | None] = mapped_column(Text)
    player_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    score_state: Mapped[str | None] = mapped_column(Text)
    minute: Mapped[int | None] = mapped_column(SmallInteger)

    token_estimate: Mapped[int | None] = mapped_column(Integer)
    built_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        head = (self.content or '')[:60].replace('\n', ' ')
        return f'<SemanticDocument {self.doc_type} match={self.match_id} {head!r}>'


# ---------------------------------------------------------------------------
# engine / session
# ---------------------------------------------------------------------------

_engines: dict[str, object] = {}


def engine_for(club: str | None = None, echo: bool = False):
    """An engine pinned to one club's schema.

    The `connect` listener is the important part. Without it a pooled
    connection keeps whatever search_path it was last left with, so a Barcelona
    session would quietly read Arsenal's tables — the failure would look like
    wrong data rather than an error, which is the worst kind.
    """
    schema = club_schema(club) if club else DEFAULT_SCHEMA
    if schema in _engines:
        return _engines[schema]

    engine = create_engine(dsn(), echo=echo, pool_pre_ping=True, future=True)

    @event.listens_for(engine, 'connect', insert=True)
    def _set_search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            # Identifier is validated by club_schema(); quoting keeps it safe
            # even so, since this runs outside SQLAlchemy's parameter binding.
            cursor.execute(f'SET search_path TO "{schema}", public')
        finally:
            cursor.close()

    _engines[schema] = engine
    return engine


def session_for(club: str | None = None):
    """A sessionmaker bound to that club's engine."""
    return sessionmaker(bind=engine_for(club), expire_on_commit=False, future=True)


def verify_search_path(club: str | None = None) -> str:
    """What schema would a session actually read? Used by tests and by the
    readiness check, because 'it looked right' is not evidence here."""
    with engine_for(club).connect() as conn:
        return conn.execute(text('SELECT current_schema()')).scalar_one()
