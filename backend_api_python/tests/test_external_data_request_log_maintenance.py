from types import SimpleNamespace

import pytest

pytest.importorskip("celery")

from app.tasks import maintenance
from app.services import external_data_request_logs as log_module
from app.services import external_data_request_settings as settings_module


class _CleanupService:
    def __init__(self, batches=None, raises=None):
        self.batches = list(batches or [0])
        self.raises = raises
        self.finished = []

    def start_cleanup_run(self): return 7
    def cleanup_expired(self, **_kwargs):
        if self.raises: raise self.raises
        return self.batches.pop(0) if self.batches else 0
    def finish_cleanup_run(self, run_id, **kwargs): self.finished.append((run_id, kwargs))


def _settings(enabled=True):
    return SimpleNamespace(cleanup_enabled=enabled, successful_retention_days=30, error_retention_days=90, cleanup_batch_size=2)


def test_cleanup_task_batches_and_records_success(monkeypatch):
    service = _CleanupService([2, 1])
    monkeypatch.setattr(log_module, "ExternalDataRequestLogService", lambda: service)
    monkeypatch.setattr(settings_module, "load_external_data_request_log_settings", lambda: _settings())
    result = maintenance.cleanup_external_data_request_logs.run()
    assert result == {"deleted_count": 3, "run_id": 7}
    assert service.finished == [(7, {"deleted_count": 3})]


def test_cleanup_task_is_disabled_or_records_failure(monkeypatch):
    monkeypatch.setattr(settings_module, "load_external_data_request_log_settings", lambda: _settings(False))
    assert maintenance.cleanup_external_data_request_logs.run()["skipped"] is True

    service = _CleanupService(raises=RuntimeError("token=secret"))
    monkeypatch.setattr(log_module, "ExternalDataRequestLogService", lambda: service)
    monkeypatch.setattr(settings_module, "load_external_data_request_log_settings", lambda: _settings())
    result = maintenance.cleanup_external_data_request_logs.run()
    assert result["run_id"] == 7
    assert "secret" not in result["error"]
    assert service.finished[0][1]["deleted_count"] == 0
