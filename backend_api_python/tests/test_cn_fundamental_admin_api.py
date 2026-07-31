from flask import Flask
from flask_smorest import Api
import pytest

from app.routes import cn_market_history_admin as routes
from app.services.task_control.repository import TaskRunRecord
from app.utils import auth


@pytest.fixture(scope="module")
def app():
    application = Flask(__name__)
    application.config.update(TESTING=True, API_TITLE="test", API_VERSION="v1", OPENAPI_VERSION="3.0.3")
    Api(application).register_blueprint(routes.cn_market_history_blp, url_prefix="/api/market-history")
    return application


def headers():
    return {"Authorization": "Bearer test-token"}


def authorize(monkeypatch, role="admin"):
    monkeypatch.setattr(auth, "verify_token", lambda _token: {
        "sub": "tester", "user_id": 7, "_verified_username": "tester",
        "_verified_user_role": role,
    })


@pytest.fixture(autouse=True)
def _internal_task_stubs(monkeypatch):
    monkeypatch.setattr(routes, "_request_task_cancel_by_domain", lambda *_args: None)
    def submit(domain_factory, **_kwargs):
        run_id = domain_factory()
        return TaskRunRecord("task-run", "cn_fundamental_backfill", "1", "queued", "cn_fundamental", {"run_id": run_id}, 3, domain_kind="cn_fundamental", domain_run_id=run_id), True
    monkeypatch.setattr(routes, "_submit_fundamental_sync", submit)


class RunService:
    def __init__(self):
        self.controls = []

    def create_run(self, instruments, **kwargs):
        assert kwargs["requested_by"] == 7
        return "fund-run"

    def set_control_status(self, run_id, status, **kwargs):
        self.controls.append((run_id, status, kwargs))

    def retry_failed(self, run_id, **kwargs):
        return "fund-retry"


def test_non_admin_cannot_create_fundamental_backfill(client, monkeypatch):
    authorize(monkeypatch, "user")
    queued = []
    response = client.post("/api/market-history/fundamentals/sync-runs", headers=headers(), json={"instruments": ["CNStock:600519.SH"]})
    assert response.status_code == 403
    assert queued == []


def test_admin_can_create_cancel_and_retry_but_not_pause_or_resume(client, monkeypatch):
    authorize(monkeypatch)
    service = RunService()
    queued = []
    monkeypatch.setattr(routes, "get_fundamental_run_service", lambda: service)
    def submit(domain_factory, **kwargs):
        run_id = domain_factory(); queued.append((run_id, kwargs))
        return TaskRunRecord("task-run", "cn_fundamental_backfill", "1", "queued", "cn_fundamental", {"run_id": run_id}, 3, domain_kind="cn_fundamental", domain_run_id=run_id), True
    monkeypatch.setattr(routes, "_submit_fundamental_sync", submit)
    created = client.post("/api/market-history/fundamentals/sync-runs", headers=headers(), json={"instruments": ["CNStock:600519.SH"]})
    assert created.status_code == 202
    assert queued == [("fund-run", {"owner_user_id": 7})]
    assert client.post("/api/market-history/fundamentals/sync-runs/fund-run/pause", headers=headers()).status_code == 404
    assert client.post("/api/market-history/fundamentals/sync-runs/fund-run/resume", headers=headers()).status_code == 404
    assert client.post("/api/market-history/fundamentals/sync-runs/fund-run/cancel", headers=headers()).status_code == 200
    retried = client.post("/api/market-history/fundamentals/sync-runs/fund-run/retry", headers=headers())
    assert retried.status_code == 202
    assert queued == [("fund-run", {"owner_user_id": 7}), ("fund-retry", {"owner_user_id": 7})]
