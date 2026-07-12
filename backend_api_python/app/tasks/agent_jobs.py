"""Celery execution for persistent Agent Gateway jobs."""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime

from app.celery_app import celery_app


SUPPORTED_KINDS = frozenset(
    {
        "backtest",
        "experiment_pipeline",
        "structured_tune",
        "ai_optimize",
    }
)


def supports_kind(kind: str) -> bool:
    return str(kind or "") in SUPPORTED_KINDS


def _execute(kind: str, payload: dict, on_progress):
    request_payload = copy.deepcopy(payload)
    if kind == "backtest":
        from app.routes.agent_v1.backtests import _run_backtest

        return _run_backtest(request_payload)

    from app.services.experiment.runner import ExperimentRunnerService

    runner = ExperimentRunnerService()
    user_id = int(request_payload.pop("__user_id", 1))
    if kind == "experiment_pipeline":
        return runner.run_pipeline(user_id=user_id, payload=request_payload)
    if kind == "structured_tune":
        return runner.run_structured_tune(user_id=user_id, payload=request_payload)
    if kind == "ai_optimize":
        return runner.run_ai_pipeline(
            user_id=user_id,
            payload=request_payload,
            on_progress=lambda value: on_progress(
                value if isinstance(value, dict) else {"value": value}
            ),
        )
    raise ValueError(f"Unsupported durable agent job kind: {kind}")


@celery_app.task(name="quantdinger.tasks.agent_job", acks_late=True)
def execute_agent_job(job_id: str) -> None:
    from app.utils import agent_jobs

    row = agent_jobs.get_job_for_worker(job_id)
    if row is None:
        raise ValueError(f"Agent job does not exist: {job_id}")
    if row.get("status") == "succeeded":
        return

    kind = str(row.get("kind") or "")
    if not supports_kind(kind):
        raise ValueError(f"Unsupported durable agent job kind: {kind}")

    agent_jobs._set_status(job_id, "running", started_at=datetime.utcnow())
    agent_jobs._publish_progress(job_id, {"phase": "running", "ts": time.time()})
    try:
        request_payload = row.get("request") or {}
        if isinstance(request_payload, str):
            request_payload = json.loads(request_payload)
        result = _execute(
            kind,
            dict(request_payload),
            lambda event: agent_jobs._publish_progress(job_id, event),
        )
        agent_jobs._set_result(job_id, result)
        agent_jobs._publish_progress(
            job_id,
            {"phase": "succeeded", "ts": time.time()},
            terminal=True,
        )
    except Exception as exc:
        agent_jobs._set_failure(job_id, str(exc))
        agent_jobs._publish_progress(
            job_id,
            {"phase": "failed", "error": str(exc)[:500], "ts": time.time()},
            terminal=True,
        )
        raise
