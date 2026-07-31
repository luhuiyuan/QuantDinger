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
