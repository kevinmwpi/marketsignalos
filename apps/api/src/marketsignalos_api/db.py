from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator


def get_database_url() -> str | None:
    return os.getenv("DATABASE_URL")


@contextmanager
def db_cursor() -> Generator[object, None, None]:
    """Context manager that yields a psycopg2 RealDictCursor.

    Commits on clean exit; rolls back on exception; always closes the
    connection.  Import is deferred so the API can start without psycopg2
    installed (the JSONL fallback path never calls this).
    """
    import psycopg2  # type: ignore[import-untyped]
    import psycopg2.extras  # type: ignore[import-untyped]

    dsn = get_database_url()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not configured")

    conn = psycopg2.connect(dsn)
    cur: object | None = None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()  # type: ignore[union-attr]
        conn.close()
