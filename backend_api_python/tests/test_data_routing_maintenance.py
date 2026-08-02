from __future__ import annotations

from app.services.data_routing.maintenance import MaintenanceStateReader


class Cursor:
    def __init__(self, active): self.value = active
    def execute(self, *args): pass
    def fetchone(self): return {"active": self.value}
    def close(self): pass


class Connection:
    def __init__(self, active): self.value = active
    def cursor(self): return Cursor(self.value)
    def __enter__(self): return self
    def __exit__(self, *args): pass


def test_maintenance_reader_keeps_last_true_state_during_database_failure():
    values = iter([Connection(True), RuntimeError("database down")])
    def factory():
        value = next(values)
        if isinstance(value, Exception): raise value
        return value
    reader = MaintenanceStateReader(factory, cache_seconds=0)
    assert reader.active() is True
    assert reader.active() is True
