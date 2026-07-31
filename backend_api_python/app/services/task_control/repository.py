"""PostgreSQL repository for Task Definitions, Schedules, and Runs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.utils.db import get_db_connection


ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "retry_wait", "cancel_requested"})
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
RUN_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"retry_wait", "cancel_requested", "succeeded", "failed"}),
    "retry_wait": frozenset({"queued", "cancelled", "failed"}),
    "cancel_requested": frozenset({"cancelled", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=True, sort_keys=True, default=str)


def _bounded_metadata(value: Mapping[str, Any] | None, max_bytes: int = 16 * 1024) -> tuple[str, bool]:
    encoded = _json(_sanitize_metadata(value or {}))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return encoded, False
    return _json({"metadata_truncated": True, "original_bytes": len(encoded.encode("utf-8"))}), True


_SENSITIVE_METADATA_KEYS = frozenset({
    "token", "access_token", "refresh_token", "authorization", "cookie",
    "password", "secret", "api_key", "apikey", "request_body", "response_body", "sql",
})


def _sanitize_metadata(value: Any, *, key: str = "") -> Any:
    if key.lower() in _SENSITIVE_METADATA_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(child_key): _sanitize_metadata(child_value, key=str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata(item) for item in value]
    return value


def validate_run_transition(current: str, target: str) -> None:
    current = str(current or "")
    target = str(target or "")
    if current not in RUN_TRANSITIONS:
        raise ValueError(f"Unknown Task Run status: {current}")
    if target not in RUN_TRANSITIONS[current]:
        raise ValueError(f"Illegal Task Run transition: {current} -> {target}")


@dataclass(frozen=True, slots=True)
class TaskRunRecord:
    run_id: str
    task_key: str
    definition_version: str
    status: str
    exclusivity_key: str
    parameters: dict[str, Any]
    priority: int
    owner_user_id: int | None = None
    schedule_id: int | None = None
    domain_kind: str = ""
    domain_run_id: str = ""
    retry_of_run_id: str | None = None
    result_summary: dict[str, Any] | None = None
    error_code: str = ""
    error_summary: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskRunRecord":
        parameters = row.get("parameters") or {}
        result = row.get("result_summary") or {}
        if isinstance(parameters, str):
            parameters = json.loads(parameters)
        if isinstance(result, str):
            result = json.loads(result)
        return cls(
            run_id=str(row["run_id"]),
            task_key=str(row["task_key"]),
            definition_version=str(row["definition_version"]),
            status=str(row["status"]),
            exclusivity_key=str(row["exclusivity_key"]),
            parameters=dict(parameters),
            priority=int(row.get("priority") or 0),
            owner_user_id=int(row["owner_user_id"]) if row.get("owner_user_id") is not None else None,
            schedule_id=int(row["schedule_id"]) if row.get("schedule_id") is not None else None,
            domain_kind=str(row.get("domain_kind") or ""),
            domain_run_id=str(row.get("domain_run_id") or ""),
            retry_of_run_id=str(row["retry_of_run_id"]) if row.get("retry_of_run_id") else None,
            result_summary=dict(result),
            error_code=str(row.get("error_code") or ""),
            error_summary=str(row.get("error_summary") or ""),
        )


class TaskControlRepository:
    def __init__(self, connection_factory: Callable = get_db_connection) -> None:
        self._connection_factory = connection_factory

    def get_run(self, run_id: str) -> TaskRunRecord | None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_task_runs WHERE run_id = %s", (str(run_id),))
                row = cur.fetchone()
                return TaskRunRecord.from_row(dict(row)) if row else None
            finally:
                cur.close()

    def create_run(
        self,
        *,
        task_key: str,
        definition_version: str,
        exclusivity_key: str,
        parameters: Mapping[str, Any] | None = None,
        priority: int = 2,
        owner_user_id: int | None = None,
        schedule_id: int | None = None,
        scheduled_at: Any = None,
        domain_kind: str = "",
        domain_run_id: str = "",
        retry_of_run_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[TaskRunRecord, bool]:
        """Return ``(run, created)`` and reuse an active exclusivity match."""
        key = str(exclusivity_key or "").strip()
        if not key:
            raise ValueError("exclusivity_key is required")
        if int(priority) not in {0, 1, 2, 3}:
            raise ValueError("priority must be between 0 and 3")
        run_id = str(run_id or uuid.uuid4().hex)

        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                cur.execute(
                    """
                    SELECT * FROM qd_task_runs
                    WHERE exclusivity_key = %s
                      AND status IN ('queued', 'running', 'retry_wait', 'cancel_requested')
                    ORDER BY id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (key,),
                )
                existing = cur.fetchone()
                if existing:
                    db.commit()
                    return TaskRunRecord.from_row(dict(existing)), False

                cur.execute(
                    """
                    INSERT INTO qd_task_runs
                        (run_id, task_key, definition_version, schedule_id, owner_user_id,
                         domain_kind, domain_run_id, exclusivity_key, priority, parameters,
                         scheduled_at, retry_of_run_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    RETURNING *
                    """,
                    (
                        run_id,
                        str(task_key),
                        str(definition_version),
                        schedule_id,
                        owner_user_id,
                        str(domain_kind or ""),
                        str(domain_run_id or ""),
                        key,
                        int(priority),
                        _json(parameters),
                        scheduled_at,
                        retry_of_run_id,
                    ),
                )
                row = cur.fetchone()
                db.commit()
                return TaskRunRecord.from_row(dict(row)), True
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def transition_run(
        self,
        run_id: str,
        target_status: str,
        *,
        result_status: str = "",
        result_summary: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_summary: str = "",
    ) -> TaskRunRecord:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_task_runs WHERE run_id = %s FOR UPDATE", (str(run_id),))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Task Run does not exist: {run_id}")
                current = str(row["status"])
                validate_run_transition(current, target_status)
                cur.execute(
                    """
                    UPDATE qd_task_runs
                    SET status = %s,
                        result_status = %s,
                        result_summary = %s::jsonb,
                        error_code = %s,
                        error_summary = %s,
                        started_at = CASE WHEN %s = 'running' THEN COALESCE(started_at, NOW()) ELSE started_at END,
                        finished_at = CASE WHEN %s IN ('succeeded', 'failed', 'cancelled') THEN NOW() ELSE finished_at END,
                        updated_at = NOW()
                    WHERE run_id = %s
                    RETURNING *
                    """,
                    (
                        target_status,
                        str(result_status or ""),
                        _json(result_summary),
                        str(error_code or "")[:120],
                        str(error_summary or "")[:2000],
                        target_status,
                        target_status,
                        str(run_id),
                    ),
                )
                updated = cur.fetchone()
                db.commit()
                return TaskRunRecord.from_row(dict(updated))
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def claim_next(self, *, holder_id: str, lease_seconds: int = 60) -> TaskRunRecord | None:
        """Claim one eligible Run in priority/FIFO order and create its lease."""
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM qd_task_runs
                        WHERE status IN ('queued', 'retry_wait')
                          AND available_at <= NOW()
                        ORDER BY priority ASC, created_at ASC, id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE qd_task_runs AS run
                    SET status = 'running',
                        attempt_no = run.attempt_no + 1,
                        heartbeat_at = NOW(),
                        started_at = COALESCE(run.started_at, NOW()),
                        updated_at = NOW()
                    FROM candidate
                    WHERE run.id = candidate.id
                    RETURNING run.*
                    """
                )
                row = cur.fetchone()
                if not row:
                    db.commit()
                    return None
                record = TaskRunRecord.from_row(dict(row))
                cur.execute(
                    """
                    INSERT INTO qd_task_leases
                        (run_id, holder_id, fencing_token, lease_expires_at, heartbeat_at)
                    VALUES (%s, %s, 1, NOW() + (%s * INTERVAL '1 second'), NOW())
                    ON CONFLICT (run_id) DO UPDATE SET
                        holder_id = EXCLUDED.holder_id,
                        fencing_token = qd_task_leases.fencing_token + 1,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        heartbeat_at = EXCLUDED.heartbeat_at,
                        updated_at = NOW()
                    """,
                    (record.run_id, str(holder_id), int(lease_seconds)),
                )
                db.commit()
                return record
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def renew_lease(self, *, run_id: str, holder_id: str, lease_seconds: int = 60) -> bool:
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_task_leases
                    SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        heartbeat_at = NOW(), updated_at = NOW()
                    WHERE run_id = %s AND holder_id = %s AND lease_expires_at > NOW()
                    """,
                    (int(lease_seconds), str(run_id), str(holder_id)),
                )
                renewed = cur.rowcount == 1
                if renewed:
                    cur.execute(
                        """
                        UPDATE qd_task_runs
                        SET heartbeat_at = NOW(), updated_at = NOW()
                        WHERE run_id = %s AND status IN ('running', 'cancel_requested')
                        """,
                        (str(run_id),),
                    )
                db.commit()
                return renewed
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def mark_stale_runs(self, *, stale_after_seconds: int = 180) -> int:
        """Fail Runs with no heartbeat and release their leases."""
        if int(stale_after_seconds) <= 0:
            raise ValueError("stale_after_seconds must be positive")
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_task_runs
                    SET status = 'failed', error_code = 'worker_lost',
                        error_summary = 'scheduler-worker heartbeat expired',
                        finished_at = NOW(), updated_at = NOW()
                    WHERE status IN ('running', 'cancel_requested')
                      AND heartbeat_at < NOW() - (%s * INTERVAL '1 second')
                    """,
                    (int(stale_after_seconds),),
                )
                count = cur.rowcount
                cur.execute(
                    """
                    DELETE FROM qd_task_leases lease
                    WHERE NOT EXISTS (
                        SELECT 1 FROM qd_task_runs run
                        WHERE run.run_id = lease.run_id
                          AND run.status IN ('running', 'cancel_requested')
                    )
                    """
                )
                db.commit()
                return int(count)
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def request_cancel(self, run_id: str) -> TaskRunRecord:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_task_runs WHERE run_id = %s FOR UPDATE", (str(run_id),))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Task Run does not exist: {run_id}")
                current = str(row["status"])
                if current in TERMINAL_RUN_STATUSES:
                    return TaskRunRecord.from_row(dict(row))
                target = "cancel_requested" if current == "running" else "cancelled"
                cur.execute(
                    "UPDATE qd_task_runs SET status = %s, updated_at = NOW(), finished_at = CASE WHEN %s = 'cancelled' THEN NOW() ELSE finished_at END WHERE run_id = %s RETURNING *",
                    (target, target, str(run_id)),
                )
                updated = cur.fetchone()
                db.commit()
                return TaskRunRecord.from_row(dict(updated))
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        severity: str = "info",
        stage: str = "",
        message: str = "",
        error_code: str = "",
        metadata: Mapping[str, Any] | None = None,
        actor: str = "system",
        correlation_id: str = "",
        external_request_id: str = "",
        holder_id: str | None = None,
    ) -> int:
        if severity not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"Unsupported Task Event severity: {severity}")
        metadata_json, truncated = _bounded_metadata(metadata)
        if truncated:
            event_type = "metadata_truncated"
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                if holder_id is not None:
                    cur.execute(
                        """
                        SELECT 1 FROM qd_task_leases
                        WHERE run_id = %s AND holder_id = %s AND lease_expires_at > NOW()
                        """,
                        (str(run_id), str(holder_id)),
                    )
                    if not cur.fetchone():
                        raise PermissionError("Task Run lease is missing or expired")
                cur.execute(
                    """
                    INSERT INTO qd_task_events
                        (run_id, event_type, severity, stage, message, error_code,
                         metadata, actor, correlation_id, external_request_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        str(run_id), str(event_type), severity, str(stage)[:120],
                        str(message)[:2000], str(error_code)[:120], metadata_json,
                        str(actor)[:32], str(correlation_id)[:255], str(external_request_id)[:120],
                    ),
                )
                event_id = int(cur.fetchone()["id"])
                db.commit()
                return event_id
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def revise_schedule(
        self,
        schedule_id: int,
        *,
        cron_expression: str,
        timezone: str,
        enabled: bool,
        parameters: Mapping[str, Any],
        updated_by: int | None,
    ) -> int:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_task_schedules
                    SET cron_expression = %s, timezone = %s, enabled = %s,
                        parameters = %s::jsonb, revision = revision + 1,
                        updated_by = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING revision
                    """,
                    (
                        str(cron_expression),
                        str(timezone),
                        bool(enabled),
                        _json(parameters),
                        updated_by,
                        int(schedule_id),
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Task Schedule does not exist: {schedule_id}")
                db.commit()
                return int(row["revision"])
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
