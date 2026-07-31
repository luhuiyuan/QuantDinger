import pytest
from flask import Flask
from flask_smorest import Api

from app.routes import task_management as routes
from app.services.task_control.registry import TaskDefinition, TaskRegistry
from app.services.task_control.repository import TaskRunRecord
from app.utils import auth


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.config.update(TESTING=True, API_TITLE="test", API_VERSION="v1", OPENAPI_VERSION="3.0.3")
    Api(application).register_blueprint(routes.task_management_blp, url_prefix="/api/task-management")
    return application


def _auth(monkeypatch, role="admin", user_id=7):
    monkeypatch.setattr(auth, "verify_token", lambda token: {"sub": "tester", "user_id": user_id, "_verified_username": "tester", "_verified_user_role": role})


def _headers():
    return {"Authorization": "Bearer test-token"}


class FakeRepository:
    def __init__(self):
        self.owner_filter = "unset"
        self.audit = []
        self.run = TaskRunRecord("run-1", "safe.task", "1", "queued", "safe.task", {}, 2, owner_user_id=7)
        self.event_filters = None

    def list_runs(self, **kwargs):
        self.owner_filter = kwargs.get("owner_user_id")
        return []

    def count_runs(self, **kwargs):
        self.owner_filter = kwargs.get("owner_user_id")
        return 17

    def create_run(self, **kwargs):
        return self.run, False

    def write_audit(self, **kwargs):
        self.audit.append(kwargs)

    def get_run(self, run_id):
        return self.run if run_id == self.run.run_id else None

    def get_run_detail(self, run_id):
        return {"run_id": run_id, "heartbeat_at": "now", "recent_events": []}

    def list_events(self, **kwargs):
        self.event_filters = kwargs
        return [{"id": 12, "run_id": "run-1"}]

    def overview(self, **kwargs):
        self.owner_filter = kwargs.get("owner_user_id")
        return {"queued": 1, "scheduler_health": None}

    def create_manual_retry(self, original_run_id, **kwargs):
        self.retry_args = {"original_run_id": original_run_id, **kwargs}
        return TaskRunRecord(
            "run-retry", self.run.task_key, kwargs["definition_version"], "queued",
            kwargs["exclusivity_key"], dict(kwargs["parameters"]), self.run.priority,
            owner_user_id=self.run.owner_user_id, domain_kind=self.run.domain_kind,
            domain_run_id=kwargs["domain_run_id"], retry_of_run_id=original_run_id,
        )

    def create_run_with_domain_factory(self, **kwargs):
        parameters, domain_run_id = kwargs["domain_factory"]()
        self.retry_args = {**kwargs, "parameters": parameters, "domain_run_id": domain_run_id}
        return TaskRunRecord(
            "run-retry", kwargs["task_key"], kwargs["definition_version"], "queued",
            kwargs["exclusivity_key"], dict(parameters), kwargs["priority"],
            owner_user_id=kwargs["owner_user_id"], domain_kind=kwargs["domain_kind"],
            domain_run_id=domain_run_id, retry_of_run_id=kwargs["retry_of_run_id"],
        ), True


def _registry():
    registry = TaskRegistry()
    registry.register(TaskDefinition("safe.task", "1", lambda parameters, reporter: {}))
    return registry


def test_user_lists_only_owned_runs(app, monkeypatch):
    _auth(monkeypatch, role="user", user_id=7)
    repo = FakeRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    response = app.test_client().get("/api/task-management/runs", headers=_headers())
    assert response.status_code == 200
    assert repo.owner_filter == 7
    assert response.get_json()["data"]["total"] == 17


def test_user_overview_is_scoped_to_owner(app, monkeypatch):
    _auth(monkeypatch, role="user", user_id=9)
    repo = FakeRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    response = app.test_client().get("/api/task-management/overview", headers=_headers())
    assert response.status_code == 200
    assert repo.owner_filter == 9


def test_user_cannot_start_system_task(app, monkeypatch):
    _auth(monkeypatch, role="user")
    monkeypatch.setattr(routes, "_repository", FakeRepository)
    response = app.test_client().post("/api/task-management/runs", headers=_headers(), json={"taskKey": "safe.task"})
    assert response.status_code == 403


def test_admin_duplicate_start_returns_existing_run(app, monkeypatch):
    _auth(monkeypatch)
    repo = FakeRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    monkeypatch.setattr(routes, "_registry", _registry)
    response = app.test_client().post("/api/task-management/runs", headers=_headers(), json={"taskKey": "safe.task"})
    assert response.status_code == 200
    assert response.get_json()["data"] == {"runId": "run-1", "created": False}
    assert repo.audit[0]["action"] == "start_run"


def test_non_owner_gets_not_found_for_run_detail(app, monkeypatch):
    _auth(monkeypatch, role="user", user_id=8)
    monkeypatch.setattr(routes, "_repository", FakeRepository)
    response = app.test_client().get("/api/task-management/runs/run-1", headers=_headers())
    assert response.status_code == 404


def test_owner_run_detail_includes_aggregated_payload(app, monkeypatch):
    _auth(monkeypatch, role="user", user_id=7)
    monkeypatch.setattr(routes, "_repository", FakeRepository)
    response = app.test_client().get("/api/task-management/runs/run-1", headers=_headers())
    assert response.status_code == 200
    assert response.get_json()["data"]["heartbeat_at"] == "now"


def test_user_log_query_is_forced_to_owner_and_keeps_cursor_filters(app, monkeypatch):
    _auth(monkeypatch, role="user", user_id=7)
    repo = FakeRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    response = app.test_client().get(
        "/api/task-management/logs?taskKey=safe.task&status=failed&provider=eastmoney&cursor=4",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert repo.event_filters["owner_user_id"] == 7
    assert repo.event_filters["task_key"] == "safe.task"
    assert repo.event_filters["run_status"] == "failed"
    assert repo.event_filters["provider"] == "eastmoney"
    assert response.get_json()["data"]["nextCursor"] == 12


class ScheduleRepository(FakeRepository):
    def __init__(self):
        super().__init__()
        self.schedule = {
            "id": 3, "task_key": "safe.task", "cron_expression": "0 2 * * *",
            "timezone": "Asia/Shanghai", "enabled": True, "parameters": {}, "revision": 1,
        }

    def get_schedule(self, schedule_id):
        return dict(self.schedule) if schedule_id == 3 else None

    def revise_schedule(self, schedule_id, **kwargs):
        self.revision = kwargs
        return 2

    def create_schedule(self, **kwargs):
        self.created_schedule = kwargs
        return 4

    def list_schedules(self, **kwargs):
        self.schedule_filters = kwargs
        return []


def test_schedule_pause_preserves_cron_and_audits_future_only_revision(app, monkeypatch):
    _auth(monkeypatch)
    repo = ScheduleRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    monkeypatch.setattr(routes, "_registry", _registry)
    response = app.test_client().put(
        "/api/task-management/schedules/3", headers=_headers(), json={"enabled": False, "reason": "maintenance"},
    )
    assert response.status_code == 200
    assert repo.revision["cron_expression"] == "0 2 * * *"
    assert repo.revision["enabled"] is False
    assert repo.audit[0]["action"] == "pause_schedule"
    assert repo.audit[0]["reason"] == "maintenance"


def test_create_disabled_schedule_persists_and_audits_disabled_state(app, monkeypatch):
    _auth(monkeypatch)
    repo = ScheduleRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    monkeypatch.setattr(routes, "_registry", _registry)
    response = app.test_client().post(
        "/api/task-management/schedules", headers=_headers(),
        json={
            "taskKey": "safe.task", "cron": "0 2 * * *",
            "timezone": "Asia/Shanghai", "enabled": False,
        },
    )
    assert response.status_code == 201
    assert repo.created_schedule["enabled"] is False
    assert repo.audit[0]["after"]["enabled"] is False


@pytest.mark.parametrize("method,path", [
    ("get", "/api/task-management/schedules?enabled=ambiguous"),
    ("put", "/api/task-management/schedules/3"),
])
def test_schedule_rejects_ambiguous_enabled_values(app, monkeypatch, method, path):
    _auth(monkeypatch)
    repo = ScheduleRepository()
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    monkeypatch.setattr(routes, "_registry", _registry)
    client = app.test_client()
    response = (
        client.get(path, headers=_headers())
        if method == "get"
        else client.put(path, headers=_headers(), json={"enabled": "ambiguous"})
    )
    assert response.status_code == 400


def test_invalid_pagination_returns_bad_request(app, monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr(routes, "_repository", FakeRepository)
    response = app.test_client().get("/api/task-management/runs?limit=invalid", headers=_headers())
    assert response.status_code == 400


def test_domain_retry_materializes_new_domain_reference(app, monkeypatch):
    _auth(monkeypatch)
    repo = FakeRepository()
    repo.run = TaskRunRecord(
        "run-1", "safe.task", "1", "cancelled", "safe.task", {"run_id": "domain-old"}, 2,
        owner_user_id=7, domain_kind="test_domain", domain_run_id="domain-old", checkpoint_ref="cp-1",
    )
    registry = TaskRegistry()
    registry.register(TaskDefinition(
        "safe.task", "1", lambda parameters, reporter: {},
        parameter_schema={
            "type": "object", "required": ["run_id"],
            "properties": {"run_id": {"type": "string"}}, "additionalProperties": False,
        },
        manual_retry_handler=lambda parameters, actor: {"run_id": f"domain-new-{actor}"},
    ))
    monkeypatch.setattr(routes, "_repository", lambda: repo)
    monkeypatch.setattr(routes, "_registry", lambda: registry)

    response = app.test_client().post(
        "/api/task-management/runs/run-1/retry", headers=_headers(),
        json={"checkpointRef": "cp-1", "reason": "operator confirmed"},
    )

    assert response.status_code == 201
    assert repo.retry_args["parameters"] == {"run_id": "domain-new-7"}
    assert repo.retry_args["domain_run_id"] == "domain-new-7"
    assert repo.retry_args["retry_of_run_id"] == "run-1"
