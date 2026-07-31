from datetime import date
from contextlib import contextmanager

import requests

from app.services.cn_fundamental_history.service import CNFundamentalHistoryService, CNFundamentalRunService
from app.services.cn_market_history.disk_guard import DiskGuardLevel, DiskGuardStatus


class Repository:
    def __init__(self):
        self.appended = []
        self.ensured = []

    def ensure_instrument(self, instrument):
        self.ensured.append(instrument)

    def append_observation(self, **kwargs):
        self.appended.append(kwargs)
        return len(self.appended)

    def load_as_of(self, instrument, as_of): return []
    def refresh_coverage(self, instrument, **kwargs): return {"last_period_end": None}


def test_announcement_later_than_as_of_is_not_written_or_calculated():
    repository = Repository()
    fetcher = lambda code: [{
        "period_end": date(2025, 12, 31), "available_at": date(2026, 4, 30),
        "raw": {"REPORT_DATE": "2025-12-31"}, "fields": {"revenue": 1},
        "source_version": "v2", "request_context": {}, "announcement_ref": {},
        "mapping_version": "test",
    }]
    result = CNFundamentalHistoryService(repository=repository, fetcher=fetcher).sync_instrument(
        "CNStock:600519.SH", as_of=date(2026, 4, 1)
    )
    assert repository.appended == []
    assert result["observationsWritten"] == 0


def test_sync_materializes_catalog_instrument_before_fetching_observations():
    repository = Repository()

    def fetcher(code):
        assert code == "300750"
        assert repository.ensured == ["CNStock:300750.SZ"]
        return []

    CNFundamentalHistoryService(
        repository=repository, fetcher=fetcher
    ).sync_instrument("CNStock:300750.SZ")


class RunRepository:
    def __init__(self):
        self.status = "pending"
        self.target_updates = []

    def get_run(self, run_id):
        return {
            "run_id": run_id, "status": self.status, "succeeded_symbols": 0,
            "failed_symbols": len([item for item in self.target_updates if item[2] == "failed"]),
            "total_symbols": 1,
            "targets": [{"instrument": "CNStock:600519.SH", "status": "pending"}],
        }

    @contextmanager
    def advisory_lock(self):
        yield True

    def set_run_status(self, run_id, status):
        self.status = status

    def set_target_status(self, run_id, instrument, status, **kwargs):
        self.target_updates.append((run_id, instrument, status, kwargs))

    def refresh_run_progress(self, run_id):
        pass


class HardDisk:
    def check(self):
        return DiskGuardStatus("/", DiskGuardLevel.HARD, 1000, 980, 20, 100, 50)


class OkDisk:
    def check(self):
        return DiskGuardStatus("/", DiskGuardLevel.OK, 1000, 100, 900, 100, 50)


def test_fundamental_hard_disk_limit_requests_safe_cancel():
    repository = RunRepository()
    safe_cancels = []
    result = CNFundamentalRunService(
        repository=repository, history_service=object(), disk_guard=HardDisk(),
    ).run("fund-run", on_safe_cancel=safe_cancels.append)
    assert result["status"] == "cancelled"
    assert repository.target_updates[0][2] == "cancelled"
    assert safe_cancels[0]["error_code"] == "cn_fundamental.disk_hard_limit"


def test_fundamental_timeout_is_classified_for_in_run_retry():
    repository = RunRepository()

    class History:
        def sync_instrument(self, instrument):
            raise requests.Timeout("provider timeout")

    result = CNFundamentalRunService(
        repository=repository, history_service=History(), disk_guard=OkDisk(),
    ).run("fund-run")
    assert result["temporary_failed"] == 1
    assert result["failed"] == 0
