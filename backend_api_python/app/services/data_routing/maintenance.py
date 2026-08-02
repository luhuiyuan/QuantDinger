"""Application entry-point guard for a cutover maintenance window."""

from __future__ import annotations

from threading import Lock
from time import monotonic


class MaintenanceStateReader:
    def __init__(self, connection_factory=None, *, cache_seconds: float = 1.0):
        if connection_factory is None:
            from app.utils.db_postgres import get_pg_connection
            connection_factory = get_pg_connection
        self.connection_factory = connection_factory
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._lock = Lock()
        self._checked_at = 0.0
        self._maintenance = False

    def active(self) -> bool:
        now = monotonic()
        with self._lock:
            if now - self._checked_at < self.cache_seconds:
                return self._maintenance
            try:
                with self.connection_factory() as db:
                    cur = db.cursor()
                    try:
                        cur.execute("SELECT EXISTS(SELECT 1 FROM qd_data_routing_cutovers WHERE status='maintenance') AS active")
                        self._maintenance = bool(cur.fetchone()["active"])
                    finally:
                        cur.close()
                self._checked_at = now
            except Exception:
                # Once maintenance is observed, database degradation must not reopen entry points.
                self._checked_at = now
            return self._maintenance


_reader: MaintenanceStateReader | None = None


def maintenance_active() -> bool:
    global _reader
    if _reader is None:
        _reader = MaintenanceStateReader()
    return _reader.active()
