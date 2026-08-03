"""
In-process async job runner for the Agent Gateway.

Backtests are CPU/IO heavy;
HTTP clients (especially LLM-driven agents) prefer "submit + poll" semantics.
We persist every job in `qd_agent_jobs` so the API survives worker restarts
and so audit can correlate jobs with the agent that triggered them.

Production deployments dispatch supported jobs to the internal Task Scheduler.

Progress streaming
------------------
Runners may accept a second positional argument `on_progress(dict)`. Each
call merges into a per-job event ring (kept in-process for low latency) and
also persists the latest snapshot in the `progress` JSONB column so a fresh
SSE client can replay where it left off.
"""
from __future__ import annotations

import inspect
import json
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


# Per-job in-process event buffer (monotonic seq → event dict).
# We only keep the most recent N events to bound memory.
_PROGRESS_RING_SIZE = 200
_progress_buffers: dict[str, deque] = {}
_progress_locks: dict[str, threading.Lock] = {}
_progress_signals: dict[str, threading.Event] = {}
_progress_global_lock = threading.Lock()


def _new_job_id() -> str:
    return uuid.uuid4().hex


def submit_job(
    *,
    user_id: int,
    agent_token_id: Optional[int],
    kind: str,
    request_payload: dict,
    runner: Callable[..., Any],
    idempotency_key: Optional[str] = None,
) -> dict:
    """Persist a backtest job and create its internal Task Run.

    The existing runner argument remains part of the public helper contract so
    callers do not need a route-level migration, but execution ownership is the
    code-registered ``agent_backtest`` Task Definition.
    """
    if kind != "backtest":
        raise ValueError(f"Unsupported durable agent job kind: {kind}")
    from app.services.task_control.builtin_tasks import register_phase_two_tasks
    from app.services.task_control.registry import default_task_registry
    from app.services.task_control.repository import TaskControlRepository

    register_phase_two_tasks(default_task_registry)
    definition = default_task_registry.get("agent_backtest")
    repository = TaskControlRepository()
    if idempotency_key:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """SELECT job_id, status, kind FROM qd_agent_jobs
                   WHERE agent_token_id = %s AND kind = %s AND idempotency_key = %s
                   ORDER BY id DESC LIMIT 1""",
                (agent_token_id, kind, idempotency_key),
            )
            existing = cur.fetchone()
            cur.close()
        if existing:
            return {
                "job_id": existing["job_id"], "status": existing["status"],
                "kind": existing["kind"], "created": False,
            }
    probe_parameters = {"job_id": "probe", "user_id": int(user_id)}
    exclusivity_key = definition.exclusivity_key(probe_parameters, int(user_id))
    active = repository.get_active_run_by_exclusivity(exclusivity_key)
    if active is not None and active.domain_run_id:
        return {
            "job_id": active.domain_run_id,
            "status": active.status,
            "kind": kind,
            "created": False,
        }

    job_id = _new_job_id()
    created_at = datetime.utcnow()
    parameters = {"job_id": job_id, "user_id": int(user_id)}
    try:
        if hasattr(repository, "create_agent_job_and_run"):
            run, created, idempotent = repository.create_agent_job_and_run(
                job_id=job_id, user_id=int(user_id), agent_token_id=agent_token_id, kind=kind,
                request_payload=request_payload, idempotency_key=idempotency_key,
                task_key=definition.task_key, definition_version=definition.definition_version,
                exclusivity_key=exclusivity_key, parameters=parameters, priority=definition.priority,
            )
        else:  # test doubles / downstream embedders predating the repository method
            run, created = repository.create_run(task_key=definition.task_key, definition_version=definition.definition_version,
                exclusivity_key=exclusivity_key, parameters=parameters, priority=definition.priority,
                owner_user_id=int(user_id), domain_kind="agent_job", domain_run_id=job_id)
            idempotent = False
        if idempotent:
            return {"job_id": run.domain_run_id if run else job_id, "status": run.status if run else "queued", "kind": kind, "created": False}
        if not created:
            return {
                "job_id": run.domain_run_id,
                "status": run.status,
                "kind": kind,
                "created": False,
            }
    except Exception:
        raise

    return {
        "job_id": job_id,
        "status": "queued",
        "kind": kind,
        "created": True,
        "created_at": created_at.isoformat() + "Z",
    }


def record_completed_job(
    *,
    user_id: int,
    agent_token_id: Optional[int],
    kind: str,
    request_payload: dict,
    result: Any,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Persist a completed synchronous agent action for idempotent replay."""
    job_id = _new_job_id()
    now = datetime.utcnow()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_agent_jobs
              (job_id, user_id, agent_token_id, kind, status, request, result,
               idempotency_key, created_at, started_at, finished_at)
            VALUES (%s, %s, %s, %s, 'succeeded', %s::jsonb, %s::jsonb, %s, %s, %s, %s)
            """,
            (
                job_id,
                int(user_id),
                agent_token_id,
                kind,
                json.dumps(request_payload, default=str),
                json.dumps(result, default=str),
                idempotency_key,
                now,
                now,
                now,
            ),
        )
        db.commit()
        cur.close()

    return {
        "job_id": job_id,
        "status": "succeeded",
        "kind": kind,
        "created_at": now.isoformat() + "Z",
    }


def _runner_accepts_progress(runner: Callable) -> bool:
    """True if `runner` declares a second positional parameter (on_progress)."""
    try:
        sig = inspect.signature(runner)
    except (TypeError, ValueError):
        return False
    params = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(params) >= 2


# ──────────────────────────── progress / streaming ────────────────────────────

def _job_signal(job_id: str) -> threading.Event:
    with _progress_global_lock:
        ev = _progress_signals.get(job_id)
        if ev is None:
            ev = threading.Event()
            _progress_signals[job_id] = ev
        return ev


def _job_buffer(job_id: str) -> tuple[deque, threading.Lock]:
    with _progress_global_lock:
        buf = _progress_buffers.get(job_id)
        if buf is None:
            buf = deque(maxlen=_PROGRESS_RING_SIZE)
            _progress_buffers[job_id] = buf
            _progress_locks[job_id] = threading.Lock()
        return buf, _progress_locks[job_id]


def _publish_progress(job_id: str, event: dict, *, terminal: bool = False) -> None:
    """Record a progress event in-memory + persist latest snapshot to DB."""
    buf, lock = _job_buffer(job_id)
    seq = (buf[-1]["seq"] + 1) if buf else 1
    record = {"seq": seq, "ts": event.get("ts") or time.time(), "data": event, "terminal": terminal}
    with lock:
        buf.append(record)
    _job_signal(job_id).set()
    # Persist last snapshot so cold reconnects see something.
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "UPDATE qd_agent_jobs SET progress = %s::jsonb WHERE job_id = %s",
                (json.dumps(event, default=str), job_id),
            )
            db.commit()
            cur.close()
    except Exception as exc:
        logger.debug(f"agent_jobs: progress persist failed for {job_id}: {exc}")


def stream_progress(job_id: str, *, since_seq: int = 0, idle_timeout_s: float = 60.0) -> Iterator[dict]:
    """Generator that yields progress events for a job until terminal.

    Yields dicts of shape `{seq, ts, data, terminal}`. Caller is responsible
    for serialization (e.g. into SSE frames).  Stops after a terminal event
    is delivered, or after `idle_timeout_s` seconds with no new events.
    """
    buf, lock = _job_buffer(job_id)
    last_seq = since_seq
    deadline = time.monotonic() + idle_timeout_s
    last_persisted = None

    while True:
        with lock:
            pending = [r for r in list(buf) if r["seq"] > last_seq]
        for rec in pending:
            yield rec
            last_seq = rec["seq"]
            last_persisted = json.dumps(rec.get("data") or {}, sort_keys=True, default=str)
            if rec.get("terminal"):
                _gc_job_state(job_id)
                return
            deadline = time.monotonic() + idle_timeout_s

        try:
            row = get_job_for_worker(job_id)
            snapshot = row.get("progress") if row else {}
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            serialized = json.dumps(snapshot, sort_keys=True, default=str)
            terminal = bool(row) and str(row.get("status") or "") in {
                "succeeded", "failed", "cancelled",
            }
            if snapshot and serialized != last_persisted:
                last_seq += 1
                record = {
                    "seq": last_seq,
                    "ts": time.time(),
                    "data": snapshot,
                    "terminal": terminal,
                }
                yield record
                last_persisted = serialized
                deadline = time.monotonic() + idle_timeout_s
                if terminal:
                    _gc_job_state(job_id)
                    return
            elif terminal:
                last_seq += 1
                yield {
                    "seq": last_seq,
                    "ts": time.time(),
                    "data": {"phase": str(row.get("status") or "failed")},
                    "terminal": True,
                }
                _gc_job_state(job_id)
                return
        except Exception as exc:
            logger.debug("agent_jobs: persisted progress poll failed for %s: %s", job_id, exc)

        # Wait for the next signal or until the idle window expires.
        ev = _job_signal(job_id)
        wait_for = max(0.0, deadline - time.monotonic())
        if wait_for == 0.0:
            return
        ev.wait(timeout=min(wait_for, 1.0))
        ev.clear()


def _gc_job_state(job_id: str) -> None:
    with _progress_global_lock:
        _progress_buffers.pop(job_id, None)
        _progress_locks.pop(job_id, None)
        _progress_signals.pop(job_id, None)


def _set_status(job_id: str, status: str, *, started_at: Optional[datetime] = None) -> bool:
    with get_db_connection() as db:
        cur = db.cursor()
        guard = " AND status <> 'cancelled'" if status == "running" else ""
        if started_at is not None:
            cur.execute(
                f"UPDATE qd_agent_jobs SET status = %s, started_at = %s WHERE job_id = %s{guard}",
                (status, started_at, job_id),
            )
        else:
            cur.execute(
                f"UPDATE qd_agent_jobs SET status = %s WHERE job_id = %s{guard}",
                (status, job_id),
            )
        db.commit()
        changed = bool(cur.rowcount)
        cur.close()
    return changed


def _set_result(job_id: str, result: Any) -> bool:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_agent_jobs
            SET status = 'succeeded', result = %s::jsonb, finished_at = NOW()
            WHERE job_id = %s AND status <> 'cancelled'
            """,
            (json.dumps(result, default=str), job_id),
        )
        changed = bool(cur.rowcount)
        db.commit()
        cur.close()
    return changed


def _set_failure(job_id: str, error: str) -> bool:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_agent_jobs
            SET status = 'failed', error = %s, finished_at = NOW()
            WHERE job_id = %s AND status <> 'cancelled'
            """,
            (error[:6000], job_id),
        )
        changed = bool(cur.rowcount)
        db.commit()
        cur.close()
    return changed


def get_job(job_id: str, *, user_id: int) -> Optional[dict]:
    """Tenant-scoped job lookup."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT job_id, user_id, agent_token_id, kind, status, request,
                   result, error, progress, created_at, started_at, finished_at
            FROM qd_agent_jobs
            WHERE job_id = %s AND user_id = %s
            """,
            (job_id, int(user_id)),
        )
        row = cur.fetchone()
        cur.close()
    return row


def get_job_for_worker(job_id: str) -> Optional[dict]:
    """Internal unscoped lookup for a worker processing an already-authorized job."""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT job_id, user_id, agent_token_id, kind, status, request,
                       result, error, progress, created_at, started_at, finished_at
                FROM qd_agent_jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            return cur.fetchone()
        finally:
            cur.close()


def list_jobs(*, user_id: int, kind: Optional[str] = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with get_db_connection() as db:
        cur = db.cursor()
        if kind:
            cur.execute(
                """
                SELECT job_id, kind, status, created_at, started_at, finished_at
                FROM qd_agent_jobs
                WHERE user_id = %s AND kind = %s
                ORDER BY id DESC LIMIT %s
                """,
                (int(user_id), kind, limit),
            )
        else:
            cur.execute(
                """
                SELECT job_id, kind, status, created_at, started_at, finished_at
                FROM qd_agent_jobs
                WHERE user_id = %s
                ORDER BY id DESC LIMIT %s
                """,
                (int(user_id), limit),
            )
        rows = cur.fetchall()
        cur.close()
    return rows or []


def count_active_jobs(*, user_id: int, agent_token_id: Optional[int] = None) -> int:
    with get_db_connection() as db:
        cur = db.cursor()
        if agent_token_id is None:
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM qd_agent_jobs
                WHERE user_id = %s AND status IN ('queued', 'running')
                """,
                (int(user_id),),
            )
        else:
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM qd_agent_jobs
                WHERE user_id = %s AND agent_token_id = %s
                  AND status IN ('queued', 'running')
                """,
                (int(user_id), int(agent_token_id)),
            )
        row = cur.fetchone() or {}
        cur.close()
    return int(row.get("count") or 0)


def cancel_job(job_id: str, *, user_id: int) -> Optional[dict]:
    """Request cancellation of a tenant-owned queued/running job.

    Internal task runners may not be force-killed safely. The durable row is
    marked immediately, and result/failure writers refuse to overwrite it.
    """
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_agent_jobs
            SET status = 'cancelled', finished_at = NOW(),
                progress = '{"phase":"cancelled"}'::jsonb
            WHERE job_id = %s AND user_id = %s
              AND status IN ('queued', 'running')
            RETURNING job_id, kind, status, created_at, started_at, finished_at
            """,
            (job_id, int(user_id)),
        )
        row = cur.fetchone()
        db.commit()
        cur.close()
    if row:
        _publish_progress(job_id, {"phase": "cancelled", "ts": time.time()}, terminal=True)
        return row
    return get_job(job_id, user_id=user_id)
