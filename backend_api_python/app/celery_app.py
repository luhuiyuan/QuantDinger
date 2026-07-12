"""Celery application with lazy Flask application context integration."""

from __future__ import annotations

import os

from celery import Celery, Task


def _redis_url(database: int) -> str:
    host = os.getenv("REDIS_HOST", "localhost")
    port = os.getenv("REDIS_PORT", "6379")
    password = os.getenv("REDIS_PASSWORD", "").strip()
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{host}:{port}/{database}"


class FlaskContextTask(Task):
    abstract = True
    _flask_app = None

    def __call__(self, *args, **kwargs):
        if self._flask_app is None:
            os.environ["QD_PROCESS_ROLE"] = "celery"
            from app import create_app

            self._flask_app = create_app(register_http_routes=False)
        with self._flask_app.app_context():
            return self.run(*args, **kwargs)


celery_app = Celery("quantdinger", task_cls=FlaskContextTask)
celery_app.conf.update(
    broker_url=os.getenv("CELERY_BROKER_URL", _redis_url(1)),
    result_backend=os.getenv("CELERY_RESULT_BACKEND", _redis_url(2)),
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=os.getenv("TZ", "Asia/Shanghai"),
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=max(1, int(os.getenv("CELERY_WORKER_PREFETCH", "1"))),
    worker_max_tasks_per_child=max(1, int(os.getenv("CELERY_MAX_TASKS_PER_CHILD", "100"))),
    task_soft_time_limit=max(60, int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "3300"))),
    task_time_limit=max(120, int(os.getenv("CELERY_TASK_TIME_LIMIT", "3600"))),
    result_expires=max(3600, int(os.getenv("CELERY_RESULT_EXPIRES", "86400"))),
    broker_transport_options={
        "visibility_timeout": max(3600, int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "7200"))),
    },
    imports=(
        "app.tasks.agent_jobs",
        "app.tasks.fast_analysis",
        "app.tasks.maintenance",
    ),
    task_routes={
        "quantdinger.tasks.fast_analysis": {"queue": "ai"},
        "quantdinger.tasks.agent_job": {"queue": "jobs"},
        "quantdinger.tasks.reflection": {"queue": "maintenance"},
        "quantdinger.tasks.ai_calibration": {"queue": "maintenance"},
        "quantdinger.tasks.market_catalog_sync": {"queue": "maintenance"},
        "quantdinger.tasks.worker_heartbeat": {"queue": "maintenance"},
        "quantdinger.tasks.cleanup_runtime_metadata": {"queue": "maintenance"},
    },
    beat_schedule={
        "reflection-cycle": {
            "task": "quantdinger.tasks.reflection",
            "schedule": max(300, int(os.getenv("REFLECTION_WORKER_INTERVAL_SEC", "86400"))),
        },
        "ai-calibration-cycle": {
            "task": "quantdinger.tasks.ai_calibration",
            "schedule": max(3600, int(os.getenv("AI_CALIBRATION_INTERVAL_SEC", "86400"))),
        },
        "market-catalog-sync": {
            "task": "quantdinger.tasks.market_catalog_sync",
            "schedule": max(900, int(os.getenv("MARKET_CATALOG_SYNC_INTERVAL_SEC", "21600"))),
        },
        "celery-worker-heartbeat": {
            "task": "quantdinger.tasks.worker_heartbeat",
            "schedule": 10.0,
        },
        "runtime-metadata-cleanup": {
            "task": "quantdinger.tasks.cleanup_runtime_metadata",
            "schedule": 86400.0,
        },
    },
)

__all__ = ["celery_app"]
