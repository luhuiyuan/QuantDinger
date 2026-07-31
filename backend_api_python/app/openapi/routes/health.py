"""Health and status routes (OpenAPI-documented via flask-smorest)."""
from datetime import datetime, timezone

from flask import Response
from flask_smorest import Blueprint

from app._version import APP_VERSION
from app.openapi.schemas.common import (
    ApiInfoSchema,
    HealthStatusSchema,
    ReadinessStatusSchema,
    WorkerHealthSchema,
)
from app.runtime.roles import current_process_role
from app.utils.db import get_db_connection
from app.observability.metrics import render_metrics

blp = Blueprint(
    "health",
    __name__,
    url_prefix="",
    description="Health checks and API identity",
)


def _health_payload():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc),
        "role": current_process_role().value,
    }


@blp.route("/", methods=["GET"])
@blp.response(200, ApiInfoSchema)
@blp.doc(summary="API root", tags=["Health"], operationId="getApiRoot")
def index():
    """Return API name, version, and running status."""
    return {
        "name": "QuantDinger Python API",
        "version": APP_VERSION,
        "status": "running",
        "timestamp": datetime.now(timezone.utc),
    }


@blp.route("/health", methods=["GET"])
@blp.response(200, HealthStatusSchema)
@blp.doc(summary="Health check", tags=["Health"], operationId="getHealth")
def health_check():
    """Liveness probe."""
    return _health_payload()


@blp.route("/api/health", methods=["GET"])
@blp.response(200, HealthStatusSchema)
@blp.doc(
    summary="Health check (compat path)",
    description="Used by Docker health checks and reverse-proxy probes.",
    tags=["Health"],
    operationId="getApiHealthCompat",
)
def api_health_check():
    """Same payload as ``GET /health``."""
    return _health_payload()


@blp.route("/api/health/ready", methods=["GET"])
@blp.response(200, ReadinessStatusSchema)
@blp.doc(summary="Readiness check", tags=["Health"], operationId="getReadiness")
def readiness_check():
    checks = {"postgres": _postgres_ready(), "task_scheduler": _task_scheduler_ready()}
    payload = _health_payload()
    payload["checks"] = checks
    if not all(checks.values()):
        payload["status"] = "unavailable"
        return payload, 503
    return payload


@blp.route("/api/health/workers", methods=["GET"])
@blp.response(200, WorkerHealthSchema)
@blp.doc(summary="Worker health", tags=["Health"], operationId="getWorkerHealth")
def worker_health_check():
    payload = _health_payload()
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT role,
                           COUNT(*) FILTER (WHERE status = 'running' AND heartbeat_at >= NOW() - INTERVAL '45 seconds') AS healthy,
                           COUNT(*) FILTER (WHERE status = 'running') AS total,
                           COUNT(*) FILTER (WHERE heartbeat_at < NOW() - INTERVAL '45 seconds') AS stale,
                           MAX(heartbeat_at) AS last_heartbeat
                    FROM qd_worker_heartbeats
                    GROUP BY role
                    ORDER BY role
                    """
                )
                payload["workers"] = [dict(row) for row in cur.fetchall()]
            finally:
                cur.close()
    except Exception:
        payload["status"] = "unavailable"
        payload["workers"] = []
        return payload, 503
    return payload


@blp.route("/metrics", methods=["GET"])
@blp.doc(
    summary="Prometheus metrics",
    tags=["Health"],
    operationId="getPrometheusMetrics",
)
def metrics():
    body, content_type = render_metrics()
    return Response(body, content_type=content_type)


def _postgres_ready() -> bool:
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
            finally:
                cur.close()
    except Exception:
        return False


def _task_scheduler_ready() -> bool:
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT EXISTS(SELECT 1 FROM qd_task_definitions WHERE status='active') definitions_ready,
                           EXISTS(
                               SELECT 1 FROM qd_worker_heartbeats
                               WHERE role='scheduler' AND status='running'
                                 AND heartbeat_at >= NOW() - INTERVAL '45 seconds'
                                 AND COALESCE(metadata_json->>'task_scheduler', '') = 'running'
                                 AND COALESCE(metadata_json->>'task_executor', '') = 'running'
                           ) worker_ready
                    """
                )
                row = cur.fetchone() or {}
                return bool(row.get("definitions_ready")) and bool(row.get("worker_ready"))
            finally:
                cur.close()
    except Exception:
        return False
