"""Private administrator API for external data provider observability."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.external_data_request_logs import ExternalDataRequestLogService
from app.services.external_data_request_settings import load_external_data_request_log_settings
from app.utils.auth import admin_required, login_required


external_data_request_logs_blp = Blueprint("external_data_request_logs", __name__)


def _parse_datetime(name: str) -> datetime | None:
    value = str(request.args.get(name) or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {name}; use ISO-8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _capability_error(exc: Exception):
    return jsonify({"code": 0, "msg": "external data request log capability is not enabled", "data": None}), 503


@external_data_request_logs_blp.route("/overview", methods=["GET"])
@login_required
@admin_required
def get_overview():
    try:
        settings = load_external_data_request_log_settings()
        return jsonify({"code": 1, "msg": "success", "data": {
            "enabled": settings.enabled,
            "overview": ExternalDataRequestLogService().overview(hours=request.args.get("hours", 24)),
        }})
    except Exception as exc:
        return _capability_error(exc)


@external_data_request_logs_blp.route("/logs", methods=["GET"])
@login_required
@admin_required
def list_logs():
    try:
        data = ExternalDataRequestLogService().list_logs(
            page=request.args.get("page", 1), page_size=request.args.get("page_size", 50),
            provider=str(request.args.get("provider") or ""), data_domain=str(request.args.get("data_domain") or ""),
            result=str(request.args.get("result") or ""), started_at=_parse_datetime("started_at"), ended_at=_parse_datetime("ended_at"),
        )
        return jsonify({"code": 1, "msg": "success", "data": data})
    except ValueError as exc:
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    except Exception as exc:
        return _capability_error(exc)


@external_data_request_logs_blp.route("/logs/<int:log_id>", methods=["GET"])
@login_required
@admin_required
def get_log(log_id: int):
    try:
        row = ExternalDataRequestLogService().get_log(log_id)
        if row is None:
            return jsonify({"code": 0, "msg": "log not found", "data": None}), 404
        return jsonify({"code": 1, "msg": "success", "data": row})
    except Exception as exc:
        return _capability_error(exc)


@external_data_request_logs_blp.route("/settings", methods=["GET"])
@login_required
@admin_required
def get_log_settings():
    try:
        settings = load_external_data_request_log_settings()
        return jsonify({"code": 1, "msg": "success", "data": {
            "enabled": settings.enabled,
            "cleanup_enabled": settings.cleanup_enabled,
            "successful_retention_days": settings.successful_retention_days,
            "error_retention_days": settings.error_retention_days,
            "cleanup_batch_size": settings.cleanup_batch_size,
            "latest_cleanup": ExternalDataRequestLogService().latest_cleanup_run(),
        }})
    except Exception as exc:
        return _capability_error(exc)
