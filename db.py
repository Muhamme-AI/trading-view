"""Supabase Postgres database connection helpers."""

import os

from dotenv import load_dotenv

load_dotenv()

_DRIVER = None


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


def _row_to_dict(row, description):
    if isinstance(row, dict):
        return row
    if description:
        return {desc[0]: val for desc, val in zip(description, row)}
    return row


class DbCursor:
    def __init__(self, cursor, driver):
        self._cursor = cursor
        self._driver = driver
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
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._driver == "psycopg2":
            return row
        return _row_to_dict(row, self._cursor.description)

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._driver == "psycopg2":
            return rows
        desc = self._cursor.description
        return [_row_to_dict(row, desc) for row in rows]


class DbConnection:
    def __init__(self, conn, driver, cursor_factory=None):
        self._conn = conn
        self._driver = driver
        self._cursor_factory = cursor_factory

    def cursor(self):
        if self._driver == "psycopg2":
            cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        else:
            cur = self._conn.cursor()
        return DbCursor(cur, self._driver)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _connect_psycopg2(url):
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(url, connect_timeout=10)
    return conn, "psycopg2", psycopg2.extras.RealDictCursor


def _connect_psycopg3(url):
    import psycopg
    from psycopg.rows import dict_row
    kwargs = {"row_factory": dict_row, "connect_timeout": 10}
    if "pooler.supabase.com" in url or ":6543" in url:
        kwargs["prepare_threshold"] = None
    conn = psycopg.connect(url, **kwargs)
    return conn, "psycopg3", None


def _connect():
    global _DRIVER
    url = get_database_url()
    for connect_fn in (_connect_psycopg2, _connect_psycopg3):
        try:
            conn, driver, cursor_factory = connect_fn(url)
            _DRIVER = driver
            return conn, driver, cursor_factory
        except ImportError:
            continue
        except Exception:
            if connect_fn is _connect_psycopg3:
                raise
            continue
    raise DatabaseError("No Postgres driver available. Install psycopg2-binary or psycopg.")


def get_db() -> DbConnection:
    try:
        conn, driver, cursor_factory = _connect()
    except DatabaseError:
        raise
    except Exception as e:
        raise DatabaseError(
            f"Could not connect to Supabase Postgres: {e}. "
            "On Vercel, use the Supabase *Connection Pooler* URI (port 6543), not the direct db.* URL."
        ) from e
    conn.autocommit = False
    return DbConnection(conn, driver, cursor_factory)


def check_connection() -> None:
    conn = get_db()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
