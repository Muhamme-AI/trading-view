"""Supabase Postgres database connection helpers."""

import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()


class DatabaseError(RuntimeError):
    """Raised when the database is unavailable or misconfigured."""


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise DatabaseError(
            "DATABASE_URL is not set. Add it in Vercel → Settings → Environment Variables "
            "(use Supabase Connection Pooler URI, not localhost)."
        )
    if "supabase.co" in url and "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def _connect():
    url = get_database_url()
    kwargs = {"row_factory": dict_row, "connect_timeout": 10}
    # Supabase pooler (PgBouncer) — required for Vercel/serverless
    if "pooler.supabase.com" in url or ":6543" in url:
        kwargs["prepare_threshold"] = None
    return psycopg.connect(url, **kwargs)


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
    try:
        conn = _connect()
    except psycopg.OperationalError as e:
        raise DatabaseError(
            f"Could not connect to Supabase Postgres: {e}. "
            "On Vercel, use the Supabase *Connection Pooler* URI (port 6543), not the direct db.* URL."
        ) from e
    conn.autocommit = False
    return DbConnection(conn)


def check_connection() -> None:
    conn = get_db()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
