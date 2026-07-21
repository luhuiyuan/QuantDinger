"""PostgreSQL advisory locks for serialized market-history synchronization."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from app.utils.db import get_db_connection


@contextmanager
def cn_history_advisory_lock(
    key: str,
    *,
    connection_factory: Callable = get_db_connection,
) -> Iterator[bool]:
    with connection_factory() as db:
        cur = db.cursor()
        acquired = False
        try:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (key,))
            row = cur.fetchone() or {}
            acquired = bool(row.get("acquired"))
            yield acquired
        finally:
            if acquired:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            cur.close()
