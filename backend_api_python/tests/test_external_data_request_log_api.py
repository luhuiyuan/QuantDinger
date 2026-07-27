from types import SimpleNamespace

from flask import Flask

from app.routes import external_data_request_logs as routes


def _raw(view):
    """Unwrap login/admin decorators so the route contract is unit-testable."""
    while hasattr(view, "__wrapped__"):
        view = view.__wrapped__
    return view


class _Service:
    def __init__(self):
        self.kwargs = None

    def overview(self, *, hours):
        return {"hours": int(hours), "external_calls": 3, "result_counts": {"timeout": 1}}

    def list_logs(self, **kwargs):
        self.kwargs = kwargs
        return {"items": [{"id": 2, "error_summary": "[REDACTED]"}], "total": 1, "page": kwargs["page"], "page_size": kwargs["page_size"]}

    def get_log(self, log_id):
        return {"id": log_id, "subject_summary": "BTC", "error_summary": "[REDACTED]"} if log_id == 2 else None

    def latest_cleanup_run(self):
        return {"status": "succeeded", "deleted_count": 4}


def test_admin_log_routes_return_enveloped_overview_list_detail_and_settings(monkeypatch):
    service = _Service()
    app = Flask(__name__)
    monkeypatch.setattr(routes, "ExternalDataRequestLogService", lambda: service)
    monkeypatch.setattr(routes, "load_external_data_request_log_settings", lambda: SimpleNamespace(enabled=True, cleanup_enabled=True, successful_retention_days=30, error_retention_days=90, cleanup_batch_size=500))

    with app.test_request_context("/overview?hours=12"):
        response = _raw(routes.get_overview)()
        assert response.get_json()["data"]["overview"]["external_calls"] == 3
    with app.test_request_context("/logs?page=2&page_size=20&provider=fred&data_domain=macro&result=timeout"):
        response = _raw(routes.list_logs)()
        assert response.get_json()["data"]["items"][0]["error_summary"] == "[REDACTED]"
        assert service.kwargs["page"] == "2"
        assert service.kwargs["provider"] == "fred"
    with app.test_request_context("/logs/2"):
        response = _raw(routes.get_log)(2)
        assert response.get_json()["code"] == 1
    with app.test_request_context("/settings"):
        response = _raw(routes.get_log_settings)()
        assert response.get_json()["data"]["latest_cleanup"]["deleted_count"] == 4


def test_log_list_rejects_bad_time_range_and_detail_reports_not_found(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr(routes, "ExternalDataRequestLogService", _Service)
    with app.test_request_context("/logs?started_at=not-a-time"):
        response, status = _raw(routes.list_logs)()
        assert status == 400
        assert response.get_json()["code"] == 0
    with app.test_request_context("/logs/9"):
        response, status = _raw(routes.get_log)(9)
        assert status == 404
        assert response.get_json()["data"] is None


def test_openapi_marks_external_request_log_endpoints_private():
    from app.openapi.register import enrich_spec
    spec = enrich_spec({"paths": {"/api/external-data-request-logs/logs": {"get": {}}}})
    assert spec["paths"]["/api/external-data-request-logs/logs"]["get"]["x-visibility"] == "private"


def test_non_admin_is_denied_by_actual_route_decorators(monkeypatch):
    from app.utils import auth
    app = Flask(__name__)
    app.add_url_rule("/logs", view_func=routes.list_logs)
    monkeypatch.setattr(auth, "verify_token", lambda _: {"_verified_user_role": "user", "_verified_username": "member", "user_id": 1})
    response = app.test_client().get("/logs", headers={"Authorization": "Bearer member"})
    assert response.status_code == 403
    assert response.get_json()["data"] is None
