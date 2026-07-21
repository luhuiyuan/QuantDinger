from datetime import date

import pytest
from flask import Flask
from flask_smorest import Api

from app.routes import cn_market_history_admin as routes
from app.services.cn_market_history.disk_guard import DiskGuardLevel, DiskGuardStatus
from app.services.cn_market_history.sync_service import (
    CNHistoryDiskBlocked,
    CNHistoryDuplicateRun,
)
from app.services.cn_market_history.models import CoverageReport
from app.services.cn_market_history.quality import QualityAssessment
from app.utils import auth


@pytest.fixture(scope="module")
def app():
    application = Flask(__name__)
    application.config.update(
        TESTING=True,
        API_TITLE="test",
        API_VERSION="v1",
        OPENAPI_VERSION="3.0.3",
    )
    Api(application).register_blueprint(
        routes.cn_market_history_blp,
        url_prefix="/api/market-history",
    )
    return application


def _headers():
    return {"Authorization": "Bearer test-token"}


def _auth(monkeypatch, role="admin"):
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda _token: {"sub": "tester", "user_id": 7, "role": role},
    )


class _SyncService:
    def __init__(self, error=None):
        self.error = error
        self.created = []
        self.cancelled = []

    def create_targeted_run(self, instruments, start_date, end_date, **kwargs):
        if self.error:
            raise self.error
        self.created.append((instruments, start_date, end_date, kwargs))
        return "run-new"

    def retry_failed_run(self, run_id, **kwargs):
        if self.error:
            raise self.error
        return f"retry-{run_id}"

    def cancel_run(self, run_id, **kwargs):
        if self.error:
            raise self.error
        self.cancelled.append((run_id, kwargs))


def test_non_admin_cannot_create_sync_run(client, monkeypatch):
    _auth(monkeypatch, role="user")
    service = _SyncService()
    monkeypatch.setattr(routes, "get_sync_service", lambda: service)

    response = client.post(
        "/api/market-history/sync-runs",
        headers=_headers(),
        json={
            "instruments": ["CNStock:600519.SH"],
            "startDate": "2026-01-01",
            "endDate": "2026-01-31",
        },
    )

    assert response.status_code == 403
    assert service.created == []


def test_admin_creates_persistent_run_and_enqueues_one_task(client, monkeypatch):
    _auth(monkeypatch)
    service = _SyncService()
    queued = []
    monkeypatch.setattr(routes, "get_sync_service", lambda: service)
    monkeypatch.setattr(routes, "_enqueue_sync", queued.append)

    response = client.post(
        "/api/market-history/sync-runs",
        headers=_headers(),
        json={
            "instruments": ["CNStock:600519.SH", "CNStock:000001.SZ"],
            "startDate": "2026-01-01",
            "endDate": "2026-01-31",
        },
    )

    assert response.status_code == 202
    assert response.get_json()["data"]["runId"] == "run-new"
    assert queued == ["run-new"]
    assert service.created[0][3]["requested_by"] == 7


def test_duplicate_active_run_returns_conflict_without_enqueue(client, monkeypatch):
    _auth(monkeypatch)
    service = _SyncService(CNHistoryDuplicateRun("run-active"))
    queued = []
    monkeypatch.setattr(routes, "get_sync_service", lambda: service)
    monkeypatch.setattr(routes, "_enqueue_sync", queued.append)

    response = client.post(
        "/api/market-history/sync-runs",
        headers=_headers(),
        json={
            "instruments": ["CNStock:600519.SH"],
            "startDate": "2026-01-01",
            "endDate": "2026-01-31",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["data"]["activeRunId"] == "run-active"
    assert queued == []


def test_invalid_sync_range_is_rejected_before_service_call(client, monkeypatch):
    _auth(monkeypatch)
    service = _SyncService()
    monkeypatch.setattr(routes, "get_sync_service", lambda: service)

    response = client.post(
        "/api/market-history/sync-runs",
        headers=_headers(),
        json={
            "instruments": ["CNStock:600519.SH"],
            "startDate": "2026-02-01",
            "endDate": "2026-01-01",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["msg"] == "cn_history.invalid_date_range"
    assert service.created == []


def test_disk_soft_limit_returns_thresholds_and_operator_action(client, monkeypatch):
    _auth(monkeypatch)
    status = DiskGuardStatus(
        path="/",
        level=DiskGuardLevel.SOFT,
        total_bytes=1000,
        used_bytes=900,
        free_bytes=100,
        soft_free_bytes=200,
        hard_free_bytes=50,
    )
    monkeypatch.setattr(
        routes,
        "get_sync_service",
        lambda: _SyncService(CNHistoryDiskBlocked(status)),
    )

    response = client.post(
        "/api/market-history/sync-runs",
        headers=_headers(),
        json={
            "instruments": ["CNStock:600519.SH"],
            "startDate": "2026-01-01",
            "endDate": "2026-01-31",
        },
    )

    assert response.status_code == 507
    data = response.get_json()["data"]
    assert data["level"] == "soft"
    assert data["freeBytes"] == 100
    assert data["operatorAction"] == "free_disk_space_and_retry"


def test_provider_health_is_read_only_and_reports_selected_host(client, monkeypatch):
    _auth(monkeypatch)

    class Repository:
        def list_provider_health(self, provider):
            assert provider == "easy_tdx"
            return [
                {
                    "host": "tdx.example",
                    "healthy": True,
                    "selected": True,
                    "latency_ms": 12.5,
                }
            ]

    monkeypatch.setattr(routes, "get_operations_repository", Repository)

    response = client.get("/api/market-history/provider-health", headers=_headers())

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["selectedHost"] == "tdx.example"
    assert data["activeProbePerformed"] is False


def test_admin_can_retry_and_cancel_runs_without_inline_execution(client, monkeypatch):
    _auth(monkeypatch)
    service = _SyncService()
    queued = []
    monkeypatch.setattr(routes, "get_sync_service", lambda: service)
    monkeypatch.setattr(routes, "_enqueue_sync", queued.append)

    retry = client.post(
        "/api/market-history/sync-runs/run-old/retry", headers=_headers()
    )
    cancel = client.post(
        "/api/market-history/sync-runs/run-old/cancel", headers=_headers()
    )

    assert retry.status_code == 202
    assert retry.get_json()["data"]["parentRunId"] == "run-old"
    assert queued == ["retry-run-old"]
    assert cancel.status_code == 200
    assert service.cancelled[0][0] == "run-old"


def test_coverage_endpoint_returns_gaps_findings_and_adjustment_states(client, monkeypatch):
    _auth(monkeypatch)
    report = CoverageReport(
        instrument="CNStock:600519.SH",
        requested_start=date(2026, 1, 1),
        requested_end=date(2026, 1, 31),
        first_trade_date=date(2026, 1, 5),
        last_trade_date=date(2026, 1, 30),
        expected_sessions=20,
        actual_sessions=19,
        complete=False,
        data_version="v3",
    )

    class Quality:
        def assess(self, instrument, start_date, end_date, *, persist):
            assert instrument == "CNStock:600519.SH"
            assert persist is False
            return QualityAssessment(report=report, findings=())

    class Repository:
        def get_coverage(self, instrument, *, mode):
            return {
                "instrument": instrument,
                "adjustment_mode": mode.value,
                "complete": mode.value == "raw",
            }

        def list_quality_findings(self, instrument, *, status):
            return [
                {
                    "instrument": instrument,
                    "status": status,
                    "finding_type": "missing_daily_bar",
                }
            ]

    monkeypatch.setattr(routes, "get_quality_service", Quality)
    monkeypatch.setattr(routes, "get_operations_repository", Repository)

    response = client.get(
        "/api/market-history/instruments/CNStock:600519.SH/coverage"
        "?startDate=2026-01-01&endDate=2026-01-31",
        headers=_headers(),
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["assessment"]["actual_sessions"] == 19
    assert data["findings"][0]["finding_type"] == "missing_daily_bar"
    assert set(data["adjustments"]) == {"raw", "forward", "backward"}


def test_disk_status_endpoint_exposes_live_threshold_decision(client, monkeypatch):
    _auth(monkeypatch)
    status = DiskGuardStatus(
        path="/",
        level=DiskGuardLevel.HARD,
        total_bytes=1000,
        used_bytes=970,
        free_bytes=30,
        soft_free_bytes=200,
        hard_free_bytes=50,
    )

    class Guard:
        def check(self):
            return status

    monkeypatch.setattr(routes, "get_disk_guard", Guard)
    response = client.get("/api/market-history/disk-status", headers=_headers())

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["level"] == "hard"
    assert data["allowsNewSync"] is False
    assert data["allowsCurrentWrite"] is False
