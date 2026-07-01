"""Session memory persisted in Postgres."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from db import get_db


def ensure_agent_schema(conn) -> None:
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            title TEXT DEFAULT 'New conversation'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_messages_session
        ON agent_messages(session_id, created_at)
    """)
    conn.commit()


def new_session_id() -> str:
    return str(uuid.uuid4())


def create_session(session_id: str | None = None, title: str = "New conversation") -> str:
    sid = session_id or new_session_id()
    conn = get_db()
    try:
        ensure_agent_schema(conn)
        conn.execute(
            "INSERT INTO agent_sessions (id, title) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (sid, title),
        )
        conn.commit()
    finally:
        conn.close()
    return sid


def touch_session(session_id: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE agent_sessions SET updated_at = NOW() WHERE id = %s",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def save_message(
    session_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> None:
    create_session(session_id)
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO agent_messages (session_id, role, content, metadata)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (session_id, role, content, json.dumps(metadata or {})),
        )
        conn.execute(
            "UPDATE agent_sessions SET updated_at = NOW() WHERE id = %s",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_history(session_id: str, limit: int = 20) -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT role, content, metadata, created_at
            FROM agent_messages
            WHERE session_id = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (session_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def history_to_langchain_messages(history: list[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in history:
        role = row["role"]
        if role in ("user", "assistant", "system"):
            pairs.append((role, row["content"]))
    return pairs
