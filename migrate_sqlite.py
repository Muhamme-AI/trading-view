#!/usr/bin/env python3
"""Migrate local trading.db (SQLite) into Supabase Postgres."""

import sqlite3
from pathlib import Path

from db import get_db, check_connection

SQLITE_PATH = Path(__file__).parent / "trading.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_events (
    id SERIAL PRIMARY KEY,
    your_name TEXT NOT NULL,
    ff_title TEXT,
    country TEXT NOT NULL,
    event_date TEXT NOT NULL,
    event_time TEXT,
    previous TEXT,
    forecast TEXT,
    actual TEXT,
    impact TEXT,
    beat_miss TEXT,
    user_note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (your_name, event_date, event_time)
);

CREATE TABLE IF NOT EXISTS price_reactions (
    id SERIAL PRIMARY KEY,
    news_event_id INTEGER NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    pip_5m REAL, pip_15m REAL, pip_30m REAL, pip_60m REAL,
    direction_5m TEXT, direction_15m TEXT,
    open_price REAL, price_5m REAL, price_15m REAL, price_30m REAL, price_60m REAL,
    fetched_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (news_event_id)
);

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    news1 TEXT, news2 TEXT, news3 TEXT,
    entry TEXT, ratio REAL, sl REAL,
    previous TEXT, forecast TEXT, actual TEXT,
    outcome TEXT, improvement TEXT,
    news_event_id INTEGER REFERENCES news_events(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS news_ratings (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id SERIAL PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    events_found INTEGER DEFAULT 0,
    events_new INTEGER DEFAULT 0,
    reactions_fetched INTEGER DEFAULT 0,
    status TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_news_events_date ON news_events (event_date);
CREATE INDEX IF NOT EXISTS idx_news_events_name ON news_events (your_name);
CREATE INDEX IF NOT EXISTS idx_trades_news_event_id ON trades (news_event_id);
"""


def ensure_schema(conn):
    cur = conn.cursor()
    for stmt in SCHEMA_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            cur.execute(stmt)
    for table in (
        "news_events", "price_reactions", "trades",
        "news_ratings", "settings", "sync_log",
    ):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    conn.commit()


def sqlite_rows(table: str) -> list[sqlite3.Row]:
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    src.close()
    return rows


def insert_rows(conn, table: str, columns: list[str], rows, conflict: str | None = None) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    if conflict:
        sql += f" ON CONFLICT {conflict} DO NOTHING"
    cur = conn.cursor()
    count = 0
    for row in rows:
        cur.execute(sql, tuple(row[c] for c in columns))
        count += cur.rowcount
    return count


def reset_sequence(conn, table: str, column: str = "id"):
    conn.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
        f"COALESCE((SELECT MAX({column}) FROM {table}), 1), true)"
    )


def main():
    if not SQLITE_PATH.exists():
        print("No trading.db found.")
        return

    print("Testing Supabase connection...")
    check_connection()
    print("Connection OK.")

    conn = get_db()
    try:
        print("Ensuring Postgres schema...")
        ensure_schema(conn)

        counts = {}

        counts["news_ratings"] = insert_rows(
            conn, "news_ratings", ["id", "name", "type", "comment"],
            sqlite_rows("news_ratings"), "(name)",
        )
        counts["settings"] = insert_rows(
            conn, "settings", ["key", "value"],
            sqlite_rows("settings"), "(key)",
        )
        counts["sync_log"] = insert_rows(
            conn, "sync_log",
            ["id", "started_at", "finished_at", "events_found", "events_new",
             "reactions_fetched", "status", "error"],
            sqlite_rows("sync_log"),
        )
        counts["news_events"] = insert_rows(
            conn, "news_events",
            ["id", "your_name", "ff_title", "country", "event_date", "event_time",
             "previous", "forecast", "actual", "impact", "beat_miss", "created_at", "user_note"],
            sqlite_rows("news_events"),
            "(your_name, event_date, event_time)",
        )
        counts["price_reactions"] = insert_rows(
            conn, "price_reactions",
            ["id", "news_event_id", "pip_5m", "pip_15m", "pip_30m", "pip_60m",
             "direction_5m", "direction_15m", "open_price", "price_5m", "price_15m",
             "price_30m", "price_60m", "fetched_at"],
            sqlite_rows("price_reactions"),
            "(news_event_id)",
        )
        counts["trades"] = insert_rows(
            conn, "trades",
            ["id", "date", "news1", "news2", "news3", "entry", "ratio", "sl",
             "previous", "forecast", "actual", "outcome", "improvement",
             "created_at", "news_event_id"],
            sqlite_rows("trades"),
        )

        for table in ("news_events", "price_reactions", "trades", "news_ratings", "sync_log"):
            reset_sequence(conn, table)

        conn.commit()

        print("\nMigration complete:")
        for table, n in counts.items():
            print(f"  {table}: {n} rows inserted")

        cur = conn.cursor()
        for table in counts:
            total = cur.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            print(f"  {table} total in Supabase: {total}")

    except Exception as e:
        conn._conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
