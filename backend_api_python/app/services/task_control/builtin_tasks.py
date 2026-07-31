"""Code-owned Phase 1 Task Definitions.

Handlers are deliberately plain Python callables that are safe to execute in
the isolated Task Run subprocess.
"""

from __future__ import annotations

import os
import socket
import json
import time
from datetime import datetime
from datetime import timedelta
from typing import Any

from .registry import TaskDefinition, TaskRegistry
from .errors import PermanentTaskError, TemporaryProviderError


def _heartbeat(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.strategy_command_repository import StrategyCommandRepository

    StrategyCommandRepository().record_worker_heartbeat(
        worker_id=f"scheduler:{parameters.get('worker_id') or socket.gethostname()}",
        role="scheduler",
        metadata={"task_run_id": reporter.run_id},
    )
    return {"status": "recorded"}


def _cleanup_runtime_metadata(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.strategy_command_repository import StrategyCommandRepository

    return StrategyCommandRepository().cleanup_runtime_metadata(
        command_retention_days=max(1, int(os.getenv("STRATEGY_COMMAND_RETENTION_DAYS", "30"))),
        heartbeat_retention_days=max(1, int(os.getenv("WORKER_HEARTBEAT_RETENTION_DAYS", "7"))),
    )


def _cleanup_external_data_logs(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.external_data_request_logs import ExternalDataRequestLogService, sanitize_summary
    from app.services.external_data_request_settings import load_external_data_request_log_settings

    settings = load_external_data_request_log_settings()
    if not settings.cleanup_enabled:
        return {"skipped": True, "reason": "disabled"}
    service = ExternalDataRequestLogService()
    run_id = service.start_cleanup_run()
    deleted = 0
    try:
        for _ in range(20):
            count = service.cleanup_expired(
                successful_retention_days=settings.successful_retention_days,
                error_retention_days=settings.error_retention_days,
                batch_size=settings.cleanup_batch_size,
            )
            deleted += count
            if count < settings.cleanup_batch_size:
                break
        service.finish_cleanup_run(run_id, deleted_count=deleted)
        return {"deleted_count": deleted, "run_id": run_id}
    except Exception as exc:
        service.finish_cleanup_run(run_id, deleted_count=deleted, error=exc)
        return {"deleted_count": deleted, "run_id": run_id, "error": sanitize_summary(exc, max_length=300)}


def _cleanup_task_events(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.task_control.repository import TaskControlRepository

    deleted = TaskControlRepository().cleanup_events(
        retention_days=max(1, int(parameters.get("retention_days") or 90)),
        batch_size=max(1, min(int(parameters.get("batch_size") or 10000), 50000)),
    )
    return {"deleted_count": deleted, "retention_days": int(parameters.get("retention_days") or 90)}


def _reflection(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    if os.getenv("ENABLE_REFLECTION_WORKER", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"skipped": True}
    from app.services.reflection import ReflectionService

    return ReflectionService().run_verification_cycle()


def _ai_calibration(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    if os.getenv("ENABLE_OFFLINE_AI_CALIBRATION", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"skipped": True}
    from app.services.ai_calibration import AICalibrationService

    service = AICalibrationService()
    results = []
    for market in os.getenv("AI_CALIBRATION_MARKETS", "Crypto").split(","):
        market = market.strip()
        if market:
            result = service.calibrate_market(
                market=market,
                lookback_days=int(os.getenv("AI_CALIBRATION_LOOKBACK_DAYS", "30")),
                min_samples=int(os.getenv("AI_CALIBRATION_MIN_SAMPLES", "80")),
            )
            if result is not None:
                results.append(result.__dict__)
    return {"markets": results}


def _market_catalog_sync(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    if os.getenv("MARKET_CATALOG_AUTO_SYNC", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"skipped": True}
    from app.services.market_catalog_sync import run_market_catalog_sync_inline

    return run_market_catalog_sync_inline("internal-task-scheduler")


def _cn_market_history_sync(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.cn_market_history.sync_service import CNMarketHistorySyncService

    result = CNMarketHistorySyncService().run(
        str(parameters["run_id"]),
        on_progress=lambda item: reporter.progress(**item),
        on_safe_cancel=lambda item: reporter.request_safe_cancel(**item),
    )
    if int(result.get("temporary_failed") or 0) and not int(result.get("failed") or 0):
        raise TemporaryProviderError("CN market history provider failed temporarily; checkpoints were preserved")
    if int(result.get("failed") or 0):
        raise PermanentTaskError("CN market history targets failed; inspect domain details")
    return result


def _cn_market_history_daily(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.cn_market_history.config import load_cn_market_history_settings
    from app.services.cn_market_history.sync_service import CNMarketHistorySyncService
    from app.services.market_schedule import latest_completed_session

    settings = load_cn_market_history_settings()
    if not settings.sync_enabled or not settings.daily_symbols:
        return {"skipped": True, "reason": "disabled_or_empty_universe"}
    end_date = latest_completed_session("CNStock").date()
    start_date = end_date - timedelta(days=settings.incremental_lookback_days)
    service = CNMarketHistorySyncService(settings=settings)
    run_id = service.create_targeted_run(settings.daily_symbols, start_date, end_date, requested_by=None, request_kind="scheduled")
    reporter.domain_reference("cn_market_history", run_id)
    result = service.run(
        run_id,
        on_progress=lambda item: reporter.progress(**item),
        on_safe_cancel=lambda item: reporter.request_safe_cancel(**item),
    )
    if int(result.get("temporary_failed") or 0) and not int(result.get("failed") or 0):
        raise TemporaryProviderError("Scheduled CN market history provider failed temporarily")
    if int(result.get("failed") or 0):
        raise PermanentTaskError("Scheduled CN market history targets failed")
    return result


def _cn_quote_refresh(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    if os.getenv("CN_QUOTE_REFRESH_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"skipped": True, "reason": "disabled"}
    from app.services.market.cn_stock_quote_snapshots import CNStockQuoteRefreshService

    return CNStockQuoteRefreshService().run(trigger_kind="scheduled")


def _cn_fundamental_incremental(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    if os.getenv("CN_FUNDAMENTAL_INCREMENTAL_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"skipped": True, "reason": "disabled"}
    symbols = [item.strip() for item in os.getenv("CN_FUNDAMENTAL_DAILY_SYMBOLS", "").split(",") if item.strip()]
    if not symbols:
        return {"skipped": True, "reason": "empty_universe"}
    from app.services.cn_fundamental_history.service import CNFundamentalRunService

    service = CNFundamentalRunService()
    run_id = service.create_run(symbols, requested_by=None, request_kind="scheduled")
    reporter.domain_reference("cn_fundamental_history", run_id)
    result = service.run(
        run_id,
        on_progress=lambda item: reporter.progress(**item),
        on_safe_cancel=lambda item: reporter.request_safe_cancel(**item),
    )
    if int(result.get("temporary_failed") or 0) and not int(result.get("failed") or 0):
        raise TemporaryProviderError("Fundamental provider failed temporarily; checkpoints were preserved")
    if int(result.get("failed") or 0):
        raise PermanentTaskError("Fundamental targets failed; inspect domain details")
    return result


def _cn_fundamental_backfill(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.cn_fundamental_history.service import CNFundamentalRunService

    result = CNFundamentalRunService().run(
        str(parameters["run_id"]),
        on_progress=lambda item: reporter.progress(**item),
        on_safe_cancel=lambda item: reporter.request_safe_cancel(**item),
    )
    if int(result.get("temporary_failed") or 0) and not int(result.get("failed") or 0):
        raise TemporaryProviderError("Fundamental provider failed temporarily; checkpoints were preserved")
    if int(result.get("failed") or 0):
        raise PermanentTaskError("Fundamental targets failed; inspect domain details")
    return result


def _cancel_cn_market_history(parameters: dict[str, Any]) -> None:
    from app.services.cn_market_history.sync_service import CNMarketHistorySyncService

    CNMarketHistorySyncService().cancel_run(str(parameters["run_id"]), requested_by=None)


def _cancel_cn_fundamental(parameters: dict[str, Any]) -> None:
    from app.services.cn_fundamental_history.service import CNFundamentalRunService

    CNFundamentalRunService().set_control_status(str(parameters["run_id"]), "cancelled", actor_user_id=None)


def _retry_cn_market_history(parameters: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    from app.services.cn_market_history.sync_service import CNMarketHistorySyncService

    run_id = CNMarketHistorySyncService().retry_failed_run(
        str(parameters["run_id"]), requested_by=actor_user_id,
    )
    return {"run_id": run_id}


def _retry_cn_fundamental(parameters: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    from app.services.cn_fundamental_history.service import CNFundamentalRunService

    run_id = CNFundamentalRunService().retry_failed(
        str(parameters["run_id"]), actor_user_id=actor_user_id,
    )
    return {"run_id": run_id}


_RUN_ID_SCHEMA = {
    "type": "object",
    "required": ["run_id"],
    "properties": {"run_id": {"type": "string"}},
    "additionalProperties": False,
}


def register_phase_one_tasks(registry: TaskRegistry) -> None:
    event_cleanup_schema = {
        "type": "object",
        "properties": {
            "retention_days": {"type": "integer"},
            "batch_size": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    definitions = (
        TaskDefinition("worker_heartbeat", "1", _heartbeat, display_name="Worker heartbeat", priority=3),
        TaskDefinition("runtime_metadata_cleanup", "1", _cleanup_runtime_metadata, display_name="Runtime metadata cleanup", priority=3),
        TaskDefinition("external_data_request_log_cleanup", "1", _cleanup_external_data_logs, display_name="External request log cleanup", priority=3),
        TaskDefinition("task_event_cleanup", "1", _cleanup_task_events, parameter_schema=event_cleanup_schema, default_parameters={"retention_days": 90, "batch_size": 10000}, display_name="Task event cleanup", priority=3),
        TaskDefinition("reflection_cycle", "1", _reflection, display_name="Reflection cycle", priority=3),
        TaskDefinition("market_catalog_sync", "1", _market_catalog_sync, display_name="Market catalog sync", priority=3),
        TaskDefinition("cn_market_history_sync", "1", _cn_market_history_sync, parameter_schema=_RUN_ID_SCHEMA, display_name="CN market history sync", priority=3, capabilities={"cancel_grace_seconds": 60}, exclusivity_key_builder=lambda p, o: "cn_market_history", cancellation_handler=_cancel_cn_market_history, manual_retry_handler=_retry_cn_market_history),
        TaskDefinition("cn_market_history_daily", "1", _cn_market_history_daily, display_name="CN market history daily", priority=3, exclusivity_key_builder=lambda p, o: "cn_market_history"),
        TaskDefinition("cn_stock_quote_refresh", "1", _cn_quote_refresh, display_name="CN stock quote refresh", priority=3, exclusivity_key_builder=lambda p, o: "cn_quote_refresh"),
        TaskDefinition("cn_fundamental_incremental", "1", _cn_fundamental_incremental, display_name="CN fundamental incremental", priority=3, exclusivity_key_builder=lambda p, o: "cn_fundamental"),
        TaskDefinition("cn_fundamental_backfill", "1", _cn_fundamental_backfill, parameter_schema=_RUN_ID_SCHEMA, display_name="CN fundamental backfill", priority=3, capabilities={"no_total_timeout": True, "provider_request_timeout_seconds": 20, "cancel_grace_seconds": 60, "max_retries": 3}, exclusivity_key_builder=lambda p, o: "cn_fundamental", cancellation_handler=_cancel_cn_fundamental, manual_retry_handler=_retry_cn_fundamental),
    )
    for definition in definitions:
        registry.register(definition)


def _agent_backtest(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.utils import agent_jobs
    from app.routes.agent_v1.backtests import _run_backtest

    job_id = str(parameters["job_id"])
    row = agent_jobs.get_job_for_worker(job_id)
    if row is None:
        raise ValueError(f"Agent job does not exist: {job_id}")
    if row.get("status") == "succeeded":
        return dict(row.get("result") or {})
    if reporter.is_cancel_requested() or row.get("status") == "cancelled":
        _cancel_agent_backtest(parameters)
        return {"status": "cancelled"}
    agent_jobs._set_status(job_id, "running", started_at=datetime.utcnow())
    agent_jobs._publish_progress(job_id, {"phase": "running", "ts": time.time()})
    request_payload = row.get("request") or {}
    if isinstance(request_payload, str):
        request_payload = json.loads(request_payload)
    try:
        result = _run_backtest(dict(request_payload))
        if reporter.is_cancel_requested():
            _cancel_agent_backtest(parameters)
            return {"status": "cancelled"}
        agent_jobs._set_result(job_id, result)
        agent_jobs._publish_progress(job_id, {"phase": "succeeded", "ts": time.time()}, terminal=True)
        return result
    except Exception as exc:
        agent_jobs._set_failure(job_id, str(exc))
        agent_jobs._publish_progress(job_id, {"phase": "failed", "error": str(exc)[:500], "ts": time.time()}, terminal=True)
        raise


def _fast_ai_analysis(parameters: dict[str, Any], reporter) -> dict[str, Any]:
    from app.services.fast_analysis_tasks import run_async_analysis_task

    if reporter.is_cancel_requested():
        _cancel_fast_analysis(parameters)
        return {"status": "cancelled"}
    result = run_async_analysis_task(**parameters)
    if result.get("error"):
        raise RuntimeError(str(result.get("error"))[:2000])
    return {"task_memory_id": parameters["task_memory_id"], "analysis_status": "succeeded"}


def _cancel_agent_backtest(parameters: dict[str, Any]) -> None:
    from app.utils import agent_jobs
    job_id = str(parameters["job_id"])
    row = agent_jobs.get_job_for_worker(job_id)
    if row and row.get("status") not in {"succeeded", "failed", "cancelled"}:
        agent_jobs._set_status(job_id, "cancelled")


def _cancel_fast_analysis(parameters: dict[str, Any]) -> None:
    from app.services.analysis_memory import get_analysis_memory
    from app.services.fast_analysis_tasks import try_refund_credits, release_inflight
    memory_id = int(parameters["task_memory_id"])
    get_analysis_memory().fail_pending_task(memory_id, "cancelled")
    try_refund_credits(int(parameters["user_id"]), int(parameters.get("credits_charged") or 0), "Auto refund: cancelled fast-analysis", f"fast-analysis-refund:{memory_id}")
    release_inflight(str(parameters.get("inflight_key") or ""))


def register_phase_two_tasks(registry: TaskRegistry) -> None:
    from app.services.fast_analysis_tasks import build_task_exclusivity_key

    agent_schema = {"type": "object", "required": ["job_id", "user_id"], "properties": {"job_id": {"type": "string"}, "user_id": {"type": "integer"}}, "additionalProperties": False}
    fast_schema = {
        "type": "object",
        "required": ["task_memory_id", "market", "symbol", "language", "timeframe", "user_id", "inflight_key"],
        "properties": {
            "task_memory_id": {"type": "integer"}, "market": {"type": "string"}, "symbol": {"type": "string"},
            "language": {"type": "string"}, "model": {"type": "string"}, "timeframe": {"type": "string"},
            "user_id": {"type": "integer"}, "inflight_key": {"type": "string"}, "credits_charged": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    definitions = (
        TaskDefinition("ai_calibration_cycle", "1", _ai_calibration, display_name="AI calibration", priority=2, capabilities={"timeout_seconds": 3600, "max_retries": 3}),
        TaskDefinition("agent_backtest", "1", _agent_backtest, parameter_schema=agent_schema, display_name="Agent backtest", priority=1, capabilities={"timeout_seconds": 7200, "max_retries": 1}, exclusivity_key_builder=lambda p, o: f"agent:{p['user_id']}:backtest", cancellation_handler=_cancel_agent_backtest),
        TaskDefinition("fast_ai_analysis", "1", _fast_ai_analysis, parameter_schema=fast_schema, display_name="Fast AI analysis", priority=1, capabilities={"timeout_seconds": 1800, "max_retries": 1}, exclusivity_key_builder=lambda p, o: build_task_exclusivity_key(p["user_id"], p["market"], p["symbol"], p["timeframe"]), cancellation_handler=_cancel_fast_analysis),
    )
    for definition in definitions:
        registry.register(definition)
