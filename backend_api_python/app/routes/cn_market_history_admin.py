"""Administrator operations for durable China A-share history."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.cn_market_history import (
    AdjustmentMode,
    CNHistoryDiskBlocked,
    CNHistoryDuplicateRun,
    CNHistorySyncDisabled,
    CNMarketHistoryOperationsRepository,
    CNMarketHistoryQualityService,
    CNMarketHistorySyncService,
    load_cn_market_history_settings,
    parse_cn_instrument,
)
from app.services.cn_market_history.disk_guard import DiskGuard
from app.utils.auth import admin_required, login_required
from app.utils.logger import get_logger


logger = get_logger(__name__)
cn_market_history_blp = Blueprint("cn_market_history_admin", __name__)


def get_sync_service() -> CNMarketHistorySyncService:
    return CNMarketHistorySyncService()


def get_operations_repository() -> CNMarketHistoryOperationsRepository:
    return CNMarketHistoryOperationsRepository()


def get_quality_service() -> CNMarketHistoryQualityService:
    return CNMarketHistoryQualityService()


def get_disk_guard() -> DiskGuard:
    return DiskGuard(load_cn_market_history_settings())


def _enqueue_sync(run_id: str) -> None:
    from app.celery_app import celery_app

    celery_app.send_task(
        "quantdinger.tasks.cn_market_history_sync",
        args=[run_id],
        queue="maintenance",
    )


def _success(data: Any = None, *, status: int = 200):
    return jsonify({"code": 1, "msg": "success", "data": _json_safe(data)}), status


@cn_market_history_blp.route("/capabilities", methods=["GET"])
@login_required
@admin_required
def capabilities():
    settings = load_cn_market_history_settings()
    repository = get_operations_repository()
    summary_reader = getattr(repository, "get_coverage_summary", None)
    coverage_summary = summary_reader() if callable(summary_reader) else {}
    return _success(
        {
            "apiVersion": 1,
            "historyEnabled": settings.enabled,
            "syncEnabled": settings.sync_enabled,
            "supportedMarket": "CNStock",
            "supportedFrequency": "1d",
            "provider": "easy_tdx",
            "maxTargetsPerRun": settings.max_targets_per_run,
            "coverageSummary": coverage_summary,
        }
    )


@cn_market_history_blp.route("/sync-runs", methods=["POST"])
@login_required
@admin_required
def create_sync_run():
    try:
        payload = request.get_json(silent=True) or {}
        instruments = payload.get("instruments") or []
        if not isinstance(instruments, list):
            raise ValueError("cn_history.instruments_required")
        start_date = _parse_date(payload.get("startDate"), "startDate")
        end_date = _parse_date(payload.get("endDate"), "endDate")
        if end_date < start_date:
            raise ValueError("cn_history.invalid_date_range")
        request_kind = str(payload.get("requestKind") or "targeted").strip().lower()
        if request_kind not in {"targeted", "repair"}:
            raise ValueError("cn_history.request_kind_unsupported")
        run_id = get_sync_service().create_targeted_run(
            instruments,
            start_date,
            end_date,
            requested_by=int(g.user_id),
            request_kind=request_kind,
        )
        _enqueue_sync(run_id)
        return _success({"runId": run_id, "status": "pending"}, status=202)
    except Exception as exc:
        return _operation_error(exc, "create")


@cn_market_history_blp.route("/sync-runs", methods=["GET"])
@login_required
@admin_required
def list_sync_runs():
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
        return _success(get_operations_repository().list_sync_runs(limit=limit))
    except Exception:
        logger.exception("CN history sync-run list failed")
        return jsonify({"code": 0, "msg": "cn_history.run_list_failed", "data": None}), 500


@cn_market_history_blp.route("/sync-runs/<string:run_id>", methods=["GET"])
@login_required
@admin_required
def get_sync_run(run_id: str):
    try:
        run = get_operations_repository().get_sync_run(run_id)
        if not run:
            return jsonify({"code": 0, "msg": "cn_history.run_not_found", "data": None}), 404
        return _success(run)
    except Exception:
        logger.exception("CN history sync-run detail failed")
        return jsonify({"code": 0, "msg": "cn_history.run_detail_failed", "data": None}), 500


@cn_market_history_blp.route("/sync-runs/<string:run_id>/retry", methods=["POST"])
@login_required
@admin_required
def retry_sync_run(run_id: str):
    try:
        retry_id = get_sync_service().retry_failed_run(
            run_id, requested_by=int(g.user_id)
        )
        _enqueue_sync(retry_id)
        return _success(
            {"runId": retry_id, "parentRunId": run_id, "status": "pending"},
            status=202,
        )
    except Exception as exc:
        return _operation_error(exc, "retry")


@cn_market_history_blp.route("/sync-runs/<string:run_id>/cancel", methods=["POST"])
@login_required
@admin_required
def cancel_sync_run(run_id: str):
    try:
        get_sync_service().cancel_run(run_id, requested_by=int(g.user_id))
        return _success({"runId": run_id, "status": "cancelled"})
    except Exception as exc:
        return _operation_error(exc, "cancel")


@cn_market_history_blp.route("/provider-health", methods=["GET"])
@login_required
@admin_required
def provider_health():
    try:
        rows = get_operations_repository().list_provider_health("easy_tdx")
        selected = next((row for row in rows if row.get("selected")), None)
        available = any(row.get("healthy") for row in rows)
        return _success(
            {
                "provider": "easy_tdx",
                "available": available,
                "selectedHost": selected.get("host") if selected else None,
                "nodes": rows,
                "activeProbePerformed": False,
            }
        )
    except Exception:
        logger.exception("CN history provider-health query failed")
        return jsonify({"code": 0, "msg": "cn_history.provider_health_failed", "data": None}), 500


@cn_market_history_blp.route("/instruments/<path:instrument>/coverage", methods=["GET"])
@login_required
@admin_required
def instrument_coverage(instrument: str):
    try:
        canonical = parse_cn_instrument(instrument).canonical
        start_date = _parse_date(request.args.get("startDate"), "startDate")
        end_date = _parse_date(request.args.get("endDate"), "endDate")
        assessment = get_quality_service().assess(
            canonical, start_date, end_date, persist=False
        )
        repository = get_operations_repository()
        coverage = {
            mode.value: repository.get_coverage(canonical, mode=mode)
            for mode in AdjustmentMode
        }
        findings = repository.list_quality_findings(canonical, status="open")
        return _success(
            {
                "instrument": canonical,
                "requestedStart": start_date,
                "requestedEnd": end_date,
                "assessment": assessment.report,
                "findings": findings,
                "adjustments": coverage,
            }
        )
    except Exception as exc:
        return _operation_error(exc, "coverage")


@cn_market_history_blp.route("/disk-status", methods=["GET"])
@login_required
@admin_required
def disk_status():
    try:
        status = get_disk_guard().check()
        return _success(
            {
                **_disk_payload(status),
                "allowsNewSync": status.allows_new_sync,
                "allowsCurrentWrite": status.allows_current_write,
                "operatorAction": (
                    "free_disk_space_and_retry"
                    if not status.allows_new_sync
                    else "none"
                ),
            }
        )
    except Exception:
        logger.exception("CN history disk-status query failed")
        return jsonify({"code": 0, "msg": "cn_history.disk_status_failed", "data": None}), 500


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"cn_history.invalid_{field}") from exc


def _operation_error(exc: Exception, action: str):
    if isinstance(exc, CNHistoryDuplicateRun):
        return jsonify(
            {
                "code": 0,
                "msg": exc.code,
                "data": {"activeRunId": exc.run_id},
            }
        ), 409
    if isinstance(exc, CNHistoryDiskBlocked):
        status = exc.status
        return jsonify(
            {
                "code": 0,
                "msg": exc.code,
                "data": _json_safe(
                    {
                        **_disk_payload(status),
                        "operatorAction": "free_disk_space_and_retry",
                    }
                ),
            }
        ), 507
    if isinstance(exc, CNHistorySyncDisabled):
        return jsonify({"code": 0, "msg": exc.code, "data": None}), 503
    if isinstance(exc, KeyError):
        return jsonify({"code": 0, "msg": "cn_history.run_not_found", "data": None}), 404
    if isinstance(exc, ValueError):
        return jsonify({"code": 0, "msg": str(exc), "data": None}), 400
    logger.exception("CN history %s operation failed", action)
    return jsonify({"code": 0, "msg": f"cn_history.{action}_failed", "data": None}), 500


def _json_safe(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _json_safe(getattr(value, key))
            for key in value.__dataclass_fields__
        }
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _disk_payload(status: Any) -> dict[str, Any]:
    return {
        "path": status.path,
        "level": status.level,
        "totalBytes": status.total_bytes,
        "usedBytes": status.used_bytes,
        "freeBytes": status.free_bytes,
        "softFreeBytes": status.soft_free_bytes,
        "hardFreeBytes": status.hard_free_bytes,
    }
