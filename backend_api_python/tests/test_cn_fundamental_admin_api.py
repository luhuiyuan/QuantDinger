from flask import Flask
from flask_smorest import Api
import pytest

from app.routes import cn_market_history_admin as routes
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
    monkeypatch.setattr(routes, "_enqueue_fundamental_sync", queued.append)
    response = client.post("/api/market-history/fundamentals/sync-runs", headers=headers(), json={"instruments": ["CNStock:600519.SH"]})
    assert response.status_code == 403
    assert queued == []


def test_admin_can_create_pause_resume_cancel_and_retry(client, monkeypatch):
    authorize(monkeypatch)
    service = RunService()
    queued = []
    monkeypatch.setattr(routes, "get_fundamental_run_service", lambda: service)
    monkeypatch.setattr(routes, "_enqueue_fundamental_sync", queued.append)
    created = client.post("/api/market-history/fundamentals/sync-runs", headers=headers(), json={"instruments": ["CNStock:600519.SH"]})
    assert created.status_code == 202
    assert queued == ["fund-run"]
    assert client.post("/api/market-history/fundamentals/sync-runs/fund-run/pause", headers=headers()).status_code == 200
    assert client.post("/api/market-history/fundamentals/sync-runs/fund-run/resume", headers=headers()).status_code == 200
    assert client.post("/api/market-history/fundamentals/sync-runs/fund-run/cancel", headers=headers()).status_code == 200
    retried = client.post("/api/market-history/fundamentals/sync-runs/fund-run/retry", headers=headers())
    assert retried.status_code == 202
    assert queued == ["fund-run", "fund-run", "fund-retry"]
