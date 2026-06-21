"""Supabase Postgres database connection helpers."""

import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError(
            "Set DATABASE_URL (or SUPABASE_DB_URL) in .env — "
            "Supabase → Project Settings → Database → Connection string (URI)"
        )
    return url


class DbCursor:
    """Cursor wrapper matching common sqlite3 usage patterns."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._cursor.execute(sql, params or ())
        self.rowcount = self._cursor.rowcount
        return self

    def executemany(self, sql, params_seq):
        self._cursor.executemany(sql, params_seq)
        self.rowcount = self._cursor.rowcount
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class DbConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return DbCursor(self._conn.cursor(row_factory=dict_row))

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db() -> DbConnection:
    conn = psycopg.connect(get_database_url(), row_factory=dict_row)
    conn.autocommit = False
    return DbConnection(conn)


def check_connection() -> None:
    conn = get_db()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
