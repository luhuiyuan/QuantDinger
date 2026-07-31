"""PostgreSQL repository for Task Definitions, Schedules, and Runs."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from app.utils.db import get_db_connection
from app.services.external_data_request_logs import sanitize_summary
from .cron import CronExpression


ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "retry_wait", "cancel_requested"})
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
MANUAL_RETRY_STATUSES = frozenset({"failed", "cancelled"})
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
    if isinstance(value, str):
        return sanitize_summary(value, max_length=2000)
    return value


def validate_run_transition(current: str, target: str) -> None:
    current = str(current or "")
    target = str(target or "")
    if current not in RUN_TRANSITIONS:
        raise ValueError(f"Unknown Task Run status: {current}")
    if target not in RUN_TRANSITIONS[current]:
        raise ValueError(f"Illegal Task Run transition: {current} -> {target}")


def validate_event_type(event_type: str) -> str:
    value = str(event_type or "")
    if not _EVENT_TYPE_PATTERN.fullmatch(value):
        raise ValueError("Task Event type must match [a-z][a-z0-9_.-]{0,79}")
    return value


@dataclass(frozen=True, slots=True)
class TaskRunRecord:
    run_id: str
    task_key: str
    definition_version: str
    status: str
    exclusivity_key: str
    parameters: dict[str, Any]
    priority: int
    attempt_no: int = 0
    owner_user_id: int | None = None
    schedule_id: int | None = None
    domain_kind: str = ""
    domain_run_id: str = ""
    retry_of_run_id: str | None = None
    result_summary: dict[str, Any] | None = None
    error_code: str = ""
    error_summary: str = ""
    stage: str = ""
    progress_current: int = 0
    progress_total: int | None = None
    progress_unit: str = ""
    progress_message: str = ""
    checkpoint_ref: str = ""
    heartbeat_at: Any = None
    last_progress_at: Any = None
    started_at: Any = None
    finished_at: Any = None
    created_at: Any = None
    updated_at: Any = None
    fencing_token: int = 0

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
            attempt_no=int(row.get("attempt_no") or 0),
            owner_user_id=int(row["owner_user_id"]) if row.get("owner_user_id") is not None else None,
            schedule_id=int(row["schedule_id"]) if row.get("schedule_id") is not None else None,
            domain_kind=str(row.get("domain_kind") or ""),
            domain_run_id=str(row.get("domain_run_id") or ""),
            retry_of_run_id=str(row["retry_of_run_id"]) if row.get("retry_of_run_id") else None,
            result_summary=dict(result),
            error_code=str(row.get("error_code") or ""),
            error_summary=str(row.get("error_summary") or ""),
            stage=str(row.get("stage") or ""),
            progress_current=int(row.get("progress_current") or 0),
            progress_total=int(row["progress_total"]) if row.get("progress_total") is not None else None,
            progress_unit=str(row.get("progress_unit") or ""),
            progress_message=str(row.get("progress_message") or ""),
            checkpoint_ref=str(row.get("checkpoint_ref") or ""),
            heartbeat_at=row.get("heartbeat_at"),
            last_progress_at=row.get("last_progress_at"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            fencing_token=int(row.get("fencing_token") or 0),
        )


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    schedule_id: int
    task_key: str
    definition_version: str
    parameters: dict[str, Any]
    priority: int
    scheduled_at: datetime
    next_scheduled_at: datetime
    timezone: str
    skip_reason: str = ""
    outcome: str = "pending"


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

    def get_run_by_domain(self, domain_kind: str, domain_run_id: str) -> TaskRunRecord | None:
        rows = self._fetchall(
            "SELECT * FROM qd_task_runs WHERE domain_kind = %s AND domain_run_id = %s ORDER BY id DESC LIMIT 1",
            (str(domain_kind), str(domain_run_id)),
        )
        return TaskRunRecord.from_row(rows[0]) if rows else None

    def get_active_run_by_exclusivity(self, exclusivity_key: str) -> TaskRunRecord | None:
        rows = self._fetchall(
            """SELECT * FROM qd_task_runs WHERE exclusivity_key = %s
               AND status IN ('queued', 'running', 'retry_wait', 'cancel_requested')
               ORDER BY id DESC LIMIT 1""",
            (str(exclusivity_key),),
        )
        return TaskRunRecord.from_row(rows[0]) if rows else None

    @contextmanager
    def submission_lock(self, key: str, *, wait: bool = False) -> Iterator[bool]:
        """Hold a cross-process guard around domain-side submission effects."""
        lock_key = f"task-submission:{str(key)}"
        with self._connection_factory() as db:
            cur = db.cursor()
            acquired = False
            try:
                function = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
                cur.execute(f"SELECT {function}(hashtext(%s)) AS acquired", (lock_key,))
                row = cur.fetchone() or {}
                acquired = True if wait else bool(row.get("acquired"))
                yield acquired
            finally:
                if acquired:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))
                db.commit()
                cur.close()

    def sync_definitions(self, definitions: tuple[Any, ...]) -> int:
        """Materialize the non-empty code registry and retire removed keys."""
        if not definitions:
            raise RuntimeError("Task Registry is empty; refusing to retire all definitions")
        keys = [str(definition.task_key) for definition in definitions]
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                for definition in definitions:
                    cur.execute(
                        """
                        INSERT INTO qd_task_definitions
                            (task_key, display_name, definition_version, parameter_schema,
                             default_parameters, capabilities, priority, max_concurrency, status, updated_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, 'active', NOW())
                        ON CONFLICT (task_key) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            definition_version = EXCLUDED.definition_version,
                            parameter_schema = EXCLUDED.parameter_schema,
                            default_parameters = EXCLUDED.default_parameters,
                            capabilities = EXCLUDED.capabilities,
                            priority = EXCLUDED.priority,
                            max_concurrency = EXCLUDED.max_concurrency,
                            status = 'active', updated_at = NOW()
                        """,
                        (
                            definition.task_key,
                            definition.display_name,
                            definition.definition_version,
                            _json(definition.parameter_schema),
                            _json(definition.default_parameters),
                            _json(definition.capabilities),
                            int(definition.priority),
                            definition.max_concurrency,
                        ),
                    )
                placeholders = ", ".join(["%s"] * len(keys))
                cur.execute(
                    f"UPDATE qd_task_definitions SET status = 'retired', updated_at = NOW() WHERE task_key NOT IN ({placeholders})",
                    tuple(keys),
                )
                count = len(definitions)
                db.commit()
                return count
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def scan_due_schedules(
        self,
        *,
        now: datetime | None = None,
        scan_lock_key: int = 728941,
        limit: int = 100,
        exclusivity_key: Callable[[ScheduleDecision], str],
    ) -> tuple[ScheduleDecision, ...]:
        """Advance due schedules and, when requested, enqueue in one transaction."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (int(scan_lock_key),))
                if not cur.fetchone()["pg_try_advisory_xact_lock"]:
                    db.commit()
                    return ()
                cur.execute(
                    """
                    SELECT schedule.*, definition.definition_version, definition.priority
                    FROM qd_task_schedules AS schedule
                    JOIN qd_task_definitions AS definition ON definition.task_key = schedule.task_key
                    WHERE schedule.enabled
                      AND definition.status = 'active'
                      AND schedule.next_scheduled_at IS NOT NULL
                      AND schedule.next_scheduled_at <= %s
                    ORDER BY schedule.next_scheduled_at ASC, schedule.id ASC
                    FOR UPDATE OF schedule
                    LIMIT %s
                    """,
                    (current, int(limit)),
                )
                rows = cur.fetchall()
                decisions: list[ScheduleDecision] = []
                for raw in rows:
                    row = dict(raw)
                    scheduled_at = row["next_scheduled_at"]
                    if scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
                    cron = CronExpression.parse(row["cron_expression"])
                    late_seconds = max(0.0, (current - scheduled_at).total_seconds())
                    skip_reason = "missed_skipped" if late_seconds > 60 else ""
                    parameters = row.get("parameters") or {}
                    if isinstance(parameters, str):
                        parameters = json.loads(parameters)
                    next_scheduled_at = cron.next_after(
                        current if skip_reason else scheduled_at,
                        row["timezone"],
                    )
                    decision = ScheduleDecision(
                        schedule_id=int(row["id"]),
                        task_key=str(row["task_key"]),
                        definition_version=str(row["definition_version"]),
                        parameters=dict(parameters),
                        priority=int(row["priority"]),
                        scheduled_at=scheduled_at,
                        next_scheduled_at=next_scheduled_at,
                        timezone=str(row["timezone"]),
                        skip_reason=skip_reason,
                    )
                    outcome = "missed" if skip_reason else "pending"
                    if not skip_reason:
                        key = str(exclusivity_key(decision) or "").strip()
                        if not key:
                            raise ValueError("Scheduled Task Definition returned an empty exclusivity key")
                        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                        cur.execute(
                            """SELECT run_id FROM qd_task_runs
                               WHERE exclusivity_key = %s
                                 AND status IN ('queued', 'running', 'retry_wait', 'cancel_requested')
                               LIMIT 1 FOR UPDATE""",
                            (key,),
                        )
                        if cur.fetchone():
                            outcome = "overlap"
                        else:
                            cur.execute(
                                """INSERT INTO qd_task_runs
                                   (run_id, task_key, definition_version, schedule_id,
                                    exclusivity_key, priority, parameters, scheduled_at)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                                (
                                    uuid.uuid4().hex, decision.task_key, decision.definition_version,
                                    decision.schedule_id, key, decision.priority,
                                    _json(decision.parameters), decision.scheduled_at,
                                ),
                            )
                            outcome = "enqueued"
                    cur.execute(
                        """
                        UPDATE qd_task_schedules
                        SET last_scheduled_at = %s, next_scheduled_at = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (scheduled_at, next_scheduled_at, int(row["id"])),
                    )
                    decisions.append(ScheduleDecision(
                        schedule_id=decision.schedule_id,
                        task_key=decision.task_key,
                        definition_version=decision.definition_version,
                        parameters=decision.parameters,
                        priority=decision.priority,
                        scheduled_at=decision.scheduled_at,
                        next_scheduled_at=decision.next_scheduled_at,
                        timezone=decision.timezone,
                        skip_reason=decision.skip_reason,
                        outcome=outcome,
                    ))
                db.commit()
                return tuple(decisions)
            except Exception:
                db.rollback()
                raise
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
        audit: Mapping[str, Any] | None = None,
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
                if audit:
                    self._write_audit_cursor(cur, **{**audit, "target_id": str(run_id)})
                db.commit()
                return TaskRunRecord.from_row(dict(row)), True
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def create_agent_job_and_run(self, *, job_id: str, user_id: int, agent_token_id: int | None,
                                 kind: str, request_payload: Mapping[str, Any], idempotency_key: str | None,
                                 task_key: str, definition_version: str, exclusivity_key: str,
                                 parameters: Mapping[str, Any], priority: int) -> tuple[TaskRunRecord | None, bool, bool]:
        """Atomically insert an Agent Job and its generic Task Run.

        Returns (run, created, idempotent_existing). The advisory key serializes
        duplicate submissions across API processes.
        """
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"agent-submit:{agent_token_id}:{kind}:{idempotency_key or exclusivity_key}",))
                if idempotency_key:
                    cur.execute("SELECT job_id, status FROM qd_agent_jobs WHERE agent_token_id=%s AND kind=%s AND idempotency_key=%s ORDER BY id DESC LIMIT 1 FOR UPDATE", (agent_token_id, kind, idempotency_key))
                    existing_job = cur.fetchone()
                    if existing_job:
                        cur.execute("SELECT * FROM qd_task_runs WHERE domain_kind='agent_job' AND domain_run_id=%s ORDER BY id DESC LIMIT 1", (str(existing_job['job_id']),))
                        existing_run = cur.fetchone()
                        db.commit()
                        return (TaskRunRecord.from_row(dict(existing_run)) if existing_run else None, False, True)
                cur.execute("INSERT INTO qd_agent_jobs (job_id,user_id,agent_token_id,kind,status,request,idempotency_key,created_at) VALUES (%s,%s,%s,%s,'queued',%s::jsonb,%s,NOW())", (job_id, int(user_id), agent_token_id, kind, _json(request_payload), idempotency_key))
                cur.execute("SELECT * FROM qd_task_runs WHERE exclusivity_key=%s AND status IN ('queued','running','retry_wait','cancel_requested') ORDER BY id DESC LIMIT 1 FOR UPDATE", (exclusivity_key,))
                existing_run = cur.fetchone()
                if existing_run:
                    db.commit()
                    return TaskRunRecord.from_row(dict(existing_run)), False, False
                cur.execute("INSERT INTO qd_task_runs (run_id,task_key,definition_version,owner_user_id,domain_kind,domain_run_id,exclusivity_key,priority,parameters) VALUES (%s,%s,%s,%s,'agent_job',%s,%s,%s,%s::jsonb) RETURNING *", (uuid.uuid4().hex, task_key, definition_version, int(user_id), job_id, exclusivity_key, int(priority), _json(parameters)))
                run = TaskRunRecord.from_row(dict(cur.fetchone()))
                db.commit()
                return run, True, False
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def create_run_with_domain_factory(
        self,
        *,
        task_key: str,
        definition_version: str,
        exclusivity_key: str,
        domain_kind: str,
        domain_factory: Callable[[], tuple[Mapping[str, Any], str]],
        priority: int = 2,
        owner_user_id: int | None = None,
        retry_of_run_id: str | None = None,
        compensate: Callable[[str], None] | None = None,
    ) -> tuple[TaskRunRecord, bool]:
        """Create domain work only after owning the generic exclusivity lock."""
        key = str(exclusivity_key or "").strip()
        if not key:
            raise ValueError("exclusivity_key is required")
        domain_run_id = ""
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
                cur.execute(
                    """SELECT * FROM qd_task_runs
                       WHERE exclusivity_key = %s
                         AND status IN ('queued', 'running', 'retry_wait', 'cancel_requested')
                       ORDER BY id DESC LIMIT 1 FOR UPDATE""",
                    (key,),
                )
                existing = cur.fetchone()
                if existing:
                    db.commit()
                    return TaskRunRecord.from_row(dict(existing)), False

                parameters, domain_run_id = domain_factory()
                cur.execute(
                    """INSERT INTO qd_task_runs
                       (run_id, task_key, definition_version, owner_user_id,
                        domain_kind, domain_run_id, exclusivity_key, priority,
                        parameters, retry_of_run_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                       RETURNING *""",
                    (
                        uuid.uuid4().hex, str(task_key), str(definition_version), owner_user_id,
                        str(domain_kind), str(domain_run_id), key, int(priority),
                        _json(parameters), retry_of_run_id,
                    ),
                )
                created = TaskRunRecord.from_row(dict(cur.fetchone()))
                db.commit()
                return created, True
            except Exception:
                db.rollback()
                if domain_run_id and compensate is not None:
                    compensate(domain_run_id)
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
        holder_id: str | None = None,
        fencing_token: int | None = None,
    ) -> TaskRunRecord:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_task_runs WHERE run_id = %s FOR UPDATE", (str(run_id),))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Task Run does not exist: {run_id}")
                self._assert_lease(cur, run_id, holder_id, fencing_token)
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
                        _json(_sanitize_metadata(result_summary or {})),
                        str(error_code or "")[:120],
                        sanitize_summary(error_summary, max_length=2000),
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

    def schedule_retry(self, run_id: str, *, error_code: str, error_summary: str, delay_seconds: float, holder_id: str | None = None, fencing_token: int | None = None) -> TaskRunRecord:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_task_runs WHERE run_id = %s FOR UPDATE", (str(run_id),))
                row = cur.fetchone()
                if not row:
                    raise KeyError(run_id)
                self._assert_lease(cur, run_id, holder_id, fencing_token)
                validate_run_transition(str(row["status"]), "retry_wait")
                cur.execute(
                    """
                    UPDATE qd_task_runs
                    SET status = 'retry_wait', error_code = %s, error_summary = %s,
                        available_at = NOW() + (%s * INTERVAL '1 second'), updated_at = NOW()
                    WHERE run_id = %s RETURNING *
                    """,
                    (str(error_code)[:120], sanitize_summary(error_summary, max_length=2000), float(delay_seconds), str(run_id)),
                )
                updated = cur.fetchone()
                db.commit()
                return TaskRunRecord.from_row(dict(updated))
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def create_manual_retry(
        self,
        original_run_id: str,
        *,
        definition_version: str,
        requested_by: int | None,
        checkpoint_ref: str = "",
        parameters: Mapping[str, Any] | None = None,
        domain_run_id: str | None = None,
        exclusivity_key: str | None = None,
    ) -> TaskRunRecord:
        original = self.get_run(original_run_id)
        if original is None:
            raise KeyError(original_run_id)
        if original.status not in MANUAL_RETRY_STATUSES:
            raise ValueError("Only failed or cancelled Task Runs can be retried manually")
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT checkpoint_ref FROM qd_task_runs WHERE run_id = %s", (str(original_run_id),))
                row = cur.fetchone() or {}
                stored_checkpoint = str(row.get("checkpoint_ref") or "")
            finally:
                cur.close()
        if checkpoint_ref and checkpoint_ref != stored_checkpoint:
            raise ValueError("checkpoint_ref does not match the original Task Run")
        retried, created = self.create_run(
            task_key=original.task_key,
            definition_version=definition_version,
            exclusivity_key=exclusivity_key or original.exclusivity_key,
            parameters=parameters if parameters is not None else original.parameters,
            priority=original.priority,
            owner_user_id=original.owner_user_id,
            domain_kind=original.domain_kind,
            domain_run_id=domain_run_id if domain_run_id is not None else original.domain_run_id,
            retry_of_run_id=original.run_id,
        )
        if not created:
            raise ValueError("An active Task Run already owns the exclusivity key")
        return retried

    def claim_next(self, *, holder_id: str, lease_seconds: int = 60, claim_lock_key: int = 728942) -> TaskRunRecord | None:
        """Claim one eligible Run in priority/FIFO order and create its lease."""
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS acquired", (int(claim_lock_key),))
                if not bool((cur.fetchone() or {}).get("acquired")):
                    db.commit()
                    return None
                cur.execute(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM qd_task_runs
                        WHERE status IN ('queued', 'retry_wait')
                          AND available_at <= NOW()
                          AND NOT EXISTS (
                              SELECT 1 FROM qd_task_runs active
                              WHERE active.status IN ('running', 'cancel_requested')
                          )
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
                cur.execute("SELECT fencing_token FROM qd_task_leases WHERE run_id = %s", (record.run_id,))
                lease_row = cur.fetchone() or {}
                db.commit()
                return replace(record, fencing_token=int(lease_row.get("fencing_token") or 0))
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def renew_lease(self, *, run_id: str, holder_id: str, lease_seconds: int = 60, fencing_token: int | None = None) -> bool:
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
                      AND (%s IS NULL OR fencing_token = %s)
                    """,
                    (int(lease_seconds), str(run_id), str(holder_id), fencing_token, fencing_token),
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

    def release_lease(self, *, run_id: str, holder_id: str, fencing_token: int | None = None) -> bool:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    "DELETE FROM qd_task_leases WHERE run_id = %s AND holder_id = %s AND (%s IS NULL OR fencing_token = %s)",
                    (str(run_id), str(holder_id), fencing_token, fencing_token),
                )
                released = cur.rowcount == 1
                db.commit()
                return released
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    @staticmethod
    def _assert_lease(cur, run_id: str, holder_id: str | None, fencing_token: int | None) -> None:
        if holder_id is None:
            return
        cur.execute(
            """SELECT 1 FROM qd_task_leases
               WHERE run_id = %s AND holder_id = %s
                 AND lease_expires_at > NOW()
                 AND (%s IS NULL OR fencing_token = %s)""",
            (str(run_id), str(holder_id), fencing_token, fencing_token),
        )
        if not cur.fetchone():
            raise PermissionError("Task Run lease is missing, expired, or fenced")

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
                # Fence linked domain state as part of the same stale transition;
                # late child writes must not resurrect work after worker loss.
                cur.execute(
                    """
                    UPDATE qd_agent_jobs j SET status='failed', error='worker_lost', finished_at=NOW()
                     WHERE j.job_id IN (SELECT parameters->>'job_id' FROM qd_task_runs WHERE error_code='worker_lost')
                       AND j.status IN ('queued','running')
                    """
                )
                cur.execute(
                    """UPDATE qd_analysis_memory m SET task_status='failed', task_error='worker_lost', updated_at=NOW()
                       WHERE m.id IN (SELECT NULLIF(parameters->>'task_memory_id','')::bigint FROM qd_task_runs WHERE error_code='worker_lost')
                         AND m.task_status='processing'"""
                )
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

    def request_cancel(self, run_id: str, *, audit: Mapping[str, Any] | None = None) -> TaskRunRecord:
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
                if audit:
                    self._write_audit_cursor(cur, **{**audit, "target_id": str(run_id)})
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
        fencing_token: int | None = None,
    ) -> int:
        if severity not in {"debug", "info", "warning", "error"}:
            raise ValueError(f"Unsupported Task Event severity: {severity}")
        event_type = validate_event_type(event_type)
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
                          AND (%s IS NULL OR fencing_token = %s)
                        """,
                        (str(run_id), str(holder_id), fencing_token, fencing_token),
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
                        sanitize_summary(message, max_length=2000), str(error_code)[:120], metadata_json,
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

    def update_progress(
        self,
        *,
        run_id: str,
        holder_id: str,
        stage: str = "",
        current: int = 0,
        total: int | None = None,
        unit: str = "",
        message: str = "",
        checkpoint_ref: str = "",
        fencing_token: int | None = None,
    ) -> bool:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_task_runs AS run
                    SET stage = %s, progress_current = %s, progress_total = %s,
                        progress_unit = %s, progress_message = %s,
                        checkpoint_ref = %s, heartbeat_at = NOW(), last_progress_at = NOW(), updated_at = NOW()
                    WHERE run.run_id = %s
                      AND run.status IN ('running', 'cancel_requested')
                      AND EXISTS (
                          SELECT 1 FROM qd_task_leases lease
                          WHERE lease.run_id = run.run_id AND lease.holder_id = %s
                          AND lease.lease_expires_at > NOW()
                          AND (%s IS NULL OR lease.fencing_token = %s)
                      )
                    """,
                    (
                        str(stage)[:120], max(0, int(current)), total,
                        str(unit)[:64], sanitize_summary(message, max_length=500), str(checkpoint_ref)[:255],
                        str(run_id), str(holder_id), fencing_token, fencing_token,
                    ),
                )
                updated = cur.rowcount == 1
                db.commit()
                return updated
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def update_domain_reference(self, *, run_id: str, holder_id: str, domain_kind: str, domain_run_id: str, fencing_token: int | None = None) -> bool:
        """Attach a bounded domain reference while the caller owns the Run lease."""
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_task_runs AS run
                    SET domain_kind = %s, domain_run_id = %s, updated_at = NOW()
                    WHERE run.run_id = %s
                      AND run.status IN ('running', 'cancel_requested')
                      AND EXISTS (
                          SELECT 1 FROM qd_task_leases lease
                          WHERE lease.run_id = run.run_id AND lease.holder_id = %s
                          AND lease.lease_expires_at > NOW()
                          AND (%s IS NULL OR lease.fencing_token = %s)
                      )
                    """,
                    (str(domain_kind)[:120], str(domain_run_id)[:120], str(run_id), str(holder_id), fencing_token, fencing_token),
                )
                updated = cur.rowcount == 1
                db.commit()
                return updated
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def cleanup_events(self, *, retention_days: int = 90, batch_size: int = 10000) -> int:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    DELETE FROM qd_task_events
                    WHERE id IN (
                        SELECT id FROM qd_task_events
                        WHERE occurred_at < NOW() - (%s * INTERVAL '1 day')
                        ORDER BY occurred_at ASC, id ASC LIMIT %s
                    )
                    """,
                    (max(1, int(retention_days)), max(1, int(batch_size))),
                )
                count = cur.rowcount
                db.commit()
                return int(count)
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def append_schedule_event(
        self,
        *,
        schedule_id: int,
        event_type: str,
        message: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        event_type = validate_event_type(event_type)
        metadata_json, truncated = _bounded_metadata(metadata)
        if truncated:
            event_type = "metadata_truncated"
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_task_events
                        (schedule_id, event_type, severity, message, metadata, actor)
                    VALUES (%s, %s, %s, %s, %s::jsonb, 'system')
                    RETURNING id
                    """,
                    (int(schedule_id), str(event_type), "info", str(message)[:2000], metadata_json),
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
        timezone_name: str,
        enabled: bool,
        parameters: Mapping[str, Any],
        updated_by: int | None,
        audit: Mapping[str, Any] | None = None,
    ) -> int:
        next_at = CronExpression.parse(cron_expression).next_after(datetime.now(timezone.utc), timezone_name)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_task_schedules
                    SET cron_expression = %s, timezone = %s, enabled = %s,
                        parameters = %s::jsonb, revision = revision + 1,
                        next_scheduled_at = %s, updated_by = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING revision
                    """,
                    (
                        str(cron_expression),
                        str(timezone_name),
                        bool(enabled),
                        _json(parameters),
                        next_at,
                        updated_by,
                        int(schedule_id),
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Task Schedule does not exist: {schedule_id}")
                if audit:
                    self._write_audit_cursor(cur, **audit)
                db.commit()
                return int(row["revision"])
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def create_schedule(self, *, task_key: str, cron_expression: str, timezone_name: str, enabled: bool, parameters: Mapping[str, Any], actor_user_id: int | None, audit: Mapping[str, Any] | None = None) -> int:
        next_at = CronExpression.parse(cron_expression).next_after(datetime.now(timezone.utc), timezone_name)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_task_schedules
                        (task_key, cron_expression, timezone, enabled, parameters, next_scheduled_at, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s) RETURNING id
                    """,
                    (
                        str(task_key), str(cron_expression), str(timezone_name), bool(enabled),
                        _json(parameters), next_at, actor_user_id, actor_user_id,
                    ),
                )
                schedule_id = int(cur.fetchone()["id"])
                if audit:
                    self._write_audit_cursor(cur, **{**audit, "target_id": str(schedule_id)})
                db.commit()
                return schedule_id
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def ensure_builtin_schedules(self, schedules: tuple[Any, ...]) -> int:
        """Create missing built-in schedules once without overwriting operator revisions."""
        created = 0
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                for schedule in schedules:
                    next_at = CronExpression.parse(schedule.cron_expression).next_after(
                        datetime.now(timezone.utc), schedule.timezone_name
                    )
                    cur.execute(
                        """
                        INSERT INTO qd_task_schedules
                            (task_key, cron_expression, timezone, enabled, parameters, next_scheduled_at)
                        SELECT %s, %s, %s, %s, %s::jsonb, %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM qd_task_schedules WHERE task_key = %s
                        )
                        """,
                        (
                            schedule.task_key, schedule.cron_expression, schedule.timezone_name,
                            bool(schedule.enabled), _json(schedule.parameters), next_at, schedule.task_key,
                        ),
                    )
                    created += int(cur.rowcount)
                db.commit()
                return created
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def list_definitions(self) -> list[dict[str, Any]]:
        return self._fetchall("SELECT * FROM qd_task_definitions ORDER BY task_key")

    def list_schedules(self, *, task_key: str = "", enabled: bool | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if task_key:
            clauses.append("task_key = %s")
            params.append(str(task_key))
        if enabled is not None:
            clauses.append("enabled = %s")
            params.append(bool(enabled))
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        return self._fetchall(
            f"SELECT * FROM qd_task_schedules WHERE {' AND '.join(clauses)} ORDER BY task_key ASC, id ASC LIMIT %s OFFSET %s",
            tuple(params),
        )

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        rows = self._fetchall("SELECT * FROM qd_task_schedules WHERE id = %s", (int(schedule_id),))
        return rows[0] if rows else None

    def list_runs(self, *, owner_user_id: int | None = None, status: str = "", task_key: str = "", error_code: str = "", created_from: str = "", created_to: str = "", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = %s")
            params.append(int(owner_user_id))
        if status:
            clauses.append("status = %s")
            params.append(str(status))
        if task_key:
            clauses.append("task_key = %s")
            params.append(str(task_key))
        if error_code:
            clauses.append("error_code = %s")
            params.append(str(error_code))
        if created_from:
            clauses.append("created_at >= %s::timestamptz")
            params.append(str(created_from))
        if created_to:
            clauses.append("created_at <= %s::timestamptz")
            params.append(str(created_to))
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        return self._fetchall(
            f"SELECT * FROM qd_task_runs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            tuple(params),
        )

    def count_runs(self, *, owner_user_id: int | None = None, status: str = "", task_key: str = "", error_code: str = "", created_from: str = "", created_to: str = "") -> int:
        clauses = ["1=1"]
        params: list[Any] = []
        for value, expression in (
            (owner_user_id, "owner_user_id = %s"), (status, "status = %s"),
            (task_key, "task_key = %s"), (error_code, "error_code = %s"),
            (created_from, "created_at >= %s::timestamptz"),
            (created_to, "created_at <= %s::timestamptz"),
        ):
            if value not in (None, ""):
                clauses.append(expression)
                params.append(value)
        rows = self._fetchall(f"SELECT COUNT(*) AS total FROM qd_task_runs WHERE {' AND '.join(clauses)}", tuple(params))
        return int(rows[0]["total"] if rows else 0)

    def list_events(self, *, run_id: str = "", event_type: str = "", error_code: str = "", task_key: str = "", run_status: str = "", owner_user_id: int | None = None, occurred_from: str = "", occurred_to: str = "", external_request_id: str = "", provider: str = "", cursor: int = 0, limit: int = 100, tail: bool = False) -> list[dict[str, Any]]:
        clauses = ["event.id > %s"]
        params: list[Any] = [max(0, int(cursor))]
        if run_id:
            clauses.append("event.run_id = %s")
            params.append(str(run_id))
        if event_type:
            clauses.append("event.event_type = %s")
            params.append(str(event_type))
        if error_code:
            clauses.append("event.error_code = %s")
            params.append(str(error_code))
        if task_key:
            clauses.append("run.task_key = %s")
            params.append(str(task_key))
        if run_status:
            clauses.append("run.status = %s")
            params.append(str(run_status))
        if owner_user_id is not None:
            clauses.append("run.owner_user_id = %s")
            params.append(int(owner_user_id))
        if occurred_from:
            clauses.append("event.occurred_at >= %s::timestamptz")
            params.append(str(occurred_from))
        if occurred_to:
            clauses.append("event.occurred_at <= %s::timestamptz")
            params.append(str(occurred_to))
        if external_request_id:
            clauses.append("event.external_request_id = %s")
            params.append(str(external_request_id))
        if provider:
            clauses.append("""(
                event.metadata ->> 'provider' = %s OR EXISTS (
                    SELECT 1 FROM qd_external_data_request_logs external_log
                    WHERE external_log.request_id = event.external_request_id
                      AND external_log.provider = %s
                )
            )""")
            params.append(str(provider))
            params.append(str(provider))
        params.append(max(1, min(int(limit), 500)))
        base_sql = f"""SELECT event.*, run.task_key, run.status AS run_status, run.owner_user_id
                FROM qd_task_events event
                LEFT JOIN qd_task_runs run ON run.run_id = event.run_id
                WHERE {' AND '.join(clauses)}"""
        if tail and int(cursor) == 0:
            sql = f"SELECT * FROM ({base_sql} ORDER BY event.id DESC LIMIT %s) recent ORDER BY id ASC"
        else:
            sql = f"{base_sql} ORDER BY event.id ASC LIMIT %s"
        return self._fetchall(sql, tuple(params))

    def get_run_detail(self, run_id: str) -> dict[str, Any] | None:
        rows = self._fetchall(
            """
            SELECT run.*, schedule.cron_expression, schedule.timezone AS schedule_timezone,
                   schedule.revision AS schedule_revision
            FROM qd_task_runs run
            LEFT JOIN qd_task_schedules schedule ON schedule.id = run.schedule_id
            WHERE run.run_id = %s
            """,
            (str(run_id),),
        )
        if not rows:
            return None
        detail = rows[0]
        detail["recent_events"] = self.list_events(run_id=str(run_id), limit=50, tail=True)
        detail["external_request_ids"] = sorted({
            str(item.get("external_request_id"))
            for item in detail["recent_events"] if item.get("external_request_id")
        })
        return detail

    def overview(self, *, owner_user_id: int | None = None) -> dict[str, Any]:
        owner_clause = "" if owner_user_id is None else "WHERE owner_user_id = %s"
        owner_params: tuple[Any, ...] = () if owner_user_id is None else (int(owner_user_id),)
        rows = self._fetchall(
            f"""
            SELECT COUNT(*) FILTER (WHERE status='queued') queued,
                   COUNT(*) FILTER (WHERE status='running') running,
                   COUNT(*) FILTER (WHERE status='retry_wait') retry_wait,
                   COUNT(*) FILTER (WHERE status='failed') failed,
                   COUNT(*) FILTER (WHERE status='cancel_requested') cancel_requested,
                   COUNT(*) FILTER (WHERE status='failed' AND error_code='worker_lost') worker_lost
            FROM qd_task_runs {owner_clause}
            """,
            owner_params,
        )
        summary = dict(rows[0]) if rows else {}
        if owner_user_id is None:
            schedule = self._fetchall("SELECT COUNT(*) FILTER (WHERE enabled) enabled, COUNT(*) total FROM qd_task_schedules")
            summary["schedules"] = dict(schedule[0]) if schedule else {"enabled": 0, "total": 0}
        else:
            summary["schedules"] = {"enabled": 0, "total": 0}
        summary["recent_events"] = self._fetchall(
            """SELECT event.* FROM qd_task_events event
               LEFT JOIN qd_task_runs run ON run.run_id = event.run_id
               WHERE (%s IS NULL OR run.owner_user_id = %s)
               ORDER BY event.occurred_at DESC, event.id DESC LIMIT 20""",
            (owner_user_id, owner_user_id),
        )
        scheduler_health = self._fetchall(
            """SELECT worker_id, status, metadata_json, heartbeat_at
               FROM qd_worker_heartbeats WHERE role='scheduler'
               ORDER BY heartbeat_at DESC LIMIT 1"""
        )
        summary["scheduler_health"] = scheduler_health[0] if scheduler_health else None
        return summary

    @staticmethod
    def _write_audit_cursor(cur, *, actor_user_id: int | None, action: str, target_type: str, target_id: str, reason: str = "", before: Mapping[str, Any] | None = None, after: Mapping[str, Any] | None = None) -> None:
        cur.execute(
            """INSERT INTO qd_task_audit
                (actor_user_id, action, target_type, target_id, reason, before_summary, after_summary)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)""",
            (actor_user_id, str(action), str(target_type), str(target_id),
             sanitize_summary(reason, max_length=1000), _json(_sanitize_metadata(before or {})),
             _json(_sanitize_metadata(after or {}))),
        )

    def write_audit(self, *, actor_user_id: int | None, action: str, target_type: str, target_id: str, reason: str = "", before: Mapping[str, Any] | None = None, after: Mapping[str, Any] | None = None) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                self._write_audit_cursor(cur, actor_user_id=actor_user_id, action=action, target_type=target_type, target_id=target_id, reason=reason, before=before, after=after)
                db.commit()
            finally:
                cur.close()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(sql, params)
                return [dict(row) for row in cur.fetchall()]
            finally:
                cur.close()
