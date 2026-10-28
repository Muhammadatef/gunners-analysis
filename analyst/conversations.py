"""Conversation persistence in the club schema (warehouse is source of truth)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import RealDictCursor, Json

from db.connection import connect, club_schema


DDL = """
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
"""


def ensure_tables(club: str | None = None) -> None:
    schema = club_schema(club) if club else 'gunners'
    with connect(schema=schema, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)


def create_conversation(club: str = 'gunners', title: str | None = None) -> str:
    ensure_tables(club)
    cid = str(uuid.uuid4())
    with connect(schema=club_schema(club)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO conversations (id, club, title) VALUES (%s, %s, %s)',
                (cid, club, title))
    return cid


def list_conversations(club: str = 'gunners', limit: int = 50) -> list[dict]:
    ensure_tables(club)
    with connect(schema=club_schema(club)) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, club, title, created_at, updated_at '
                'FROM conversations WHERE club = %s '
                'ORDER BY updated_at DESC LIMIT %s',
                (club, limit))
            return [dict(r) for r in cur.fetchall()]


def get_conversation(conversation_id: str, club: str = 'gunners') -> dict | None:
    ensure_tables(club)
    with connect(schema=club_schema(club)) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'SELECT id, club, title, created_at, updated_at '
                'FROM conversations WHERE id = %s',
                (conversation_id,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                'SELECT * FROM messages WHERE conversation_id = %s '
                'ORDER BY created_at',
                (conversation_id,))
            msgs = [dict(r) for r in cur.fetchall()]
            out = dict(row)
            out['messages'] = msgs
            return out


def delete_conversation(conversation_id: str, club: str = 'gunners') -> bool:
    ensure_tables(club)
    with connect(schema=club_schema(club)) as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM conversations WHERE id = %s', (conversation_id,))
            return cur.rowcount > 0


def set_title(conversation_id: str, title: str, club: str = 'gunners') -> None:
    with connect(schema=club_schema(club)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE conversations SET title = %s, updated_at = now() WHERE id = %s',
                (title[:120], conversation_id))


def add_message(conversation_id: str, role: str, content: str,
                club: str = 'gunners', **meta: Any) -> str:
    ensure_tables(club)
    mid = str(uuid.uuid4())
    params = meta.get('parameters')
    evidence = meta.get('evidence')
    retrieved = meta.get('retrieved')
    with connect(schema=club_schema(club)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''INSERT INTO messages (
                     id, conversation_id, role, content, tool, query_id,
                     parameters, evidence, evidence_numbers, unsupported_numbers,
                     retrieved, reasoning, latency_s, cost_usd, mlflow_run_id
                   ) VALUES (
                     %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                   )''',
                (
                    mid, conversation_id, role, content,
                    meta.get('tool'), meta.get('query_id'),
                    Json(params) if params is not None else None,
                    Json(evidence) if evidence is not None else None,
                    meta.get('evidence_numbers'),
                    meta.get('unsupported_numbers'),
                    Json(retrieved) if retrieved is not None else None,
                    meta.get('reasoning'),
                    meta.get('latency_s'),
                    meta.get('cost_usd'),
                    meta.get('mlflow_run_id'),
                ))
            cur.execute(
                'UPDATE conversations SET updated_at = now() WHERE id = %s',
                (conversation_id,))
    return mid


def latest_slots(conversation_id: str, club: str = 'gunners') -> dict:
    """The fixture / player context of the last answered turn.

    Follow-ups ("what about him in that match?") need to know what "that match"
    was. Carrying the previous turn's *parameters* rather than its text keeps
    the prompt the same size on turn ten as on turn one, and keeps the rule
    that numbers come from tools — a slot is an input to a query, never a fact
    the model may repeat.
    """
    from agent.routing import slots_from_message

    try:
        with connect(schema=club_schema(club)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT parameters FROM messages "
                    "WHERE conversation_id = %s AND role = 'assistant' "
                    "  AND parameters IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    (conversation_id,))
                row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return {}
    return slots_from_message(dict(row)) if row else {}
