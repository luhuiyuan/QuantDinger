"""Isolated subprocess executor for one internal Task Run."""

from __future__ import annotations

import multiprocessing
import os
import signal
import ctypes
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .registry import TaskDefinition, TaskRegistry
from .repository import TaskControlRepository, TaskRunRecord
from .errors import is_temporary_error, retry_delay_seconds


logger = logging.getLogger(__name__)


class ChildReporter:
    def __init__(self, connection, run_id: str, cancel_event) -> None:
        self._connection = connection
        self.run_id = run_id
        self._cancel_event = cancel_event

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def event(self, event_type: str, *, message: str = "", severity: str = "info", metadata: Mapping[str, Any] | None = None, error_code: str = "") -> None:
        self._connection.send({"kind": "event", "event_type": event_type, "message": message, "severity": severity, "metadata": dict(metadata or {}), "error_code": error_code})

    def progress(self, *, stage: str = "", current: int = 0, total: int | None = None, unit: str = "", message: str = "", checkpoint_ref: str = "") -> None:
        self._connection.send({"kind": "progress", "stage": stage, "current": current, "total": total, "unit": unit, "message": message, "checkpoint_ref": checkpoint_ref})

    def domain_reference(self, domain_kind: str, domain_run_id: str) -> None:
        self._connection.send({
            "kind": "domain_reference",
            "domain_kind": str(domain_kind),
            "domain_run_id": str(domain_run_id),
        })

    def request_safe_cancel(self, *, error_code: str, message: str = "", metadata: Mapping[str, Any] | None = None) -> None:
        self._connection.send({
            "kind": "safe_cancel", "error_code": str(error_code),
            "message": str(message), "metadata": dict(metadata or {}),
        })


def _run_child(handler, parameters: dict[str, Any], run_id: str, connection, cancel_event) -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    if sys_platform_linux := (os.name == "posix" and hasattr(os, "setsid")):
        try:
            libc = ctypes.CDLL(None)
            libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
        except Exception:
            pass
    reporter = ChildReporter(connection, run_id, cancel_event)
    context_token = None
    try:
        from app import create_app
        from app.observability.context import request_id_context

        app = create_app(register_http_routes=False)
        with app.app_context():
            context_token = request_id_context.set(run_id)
            result = handler(parameters, reporter)
        connection.send({"kind": "result", "result": result if isinstance(result, dict) else {"value": result}})
    except Exception as exc:
        connection.send({"kind": "error", "error_code": type(exc).__name__, "message": str(exc)[:2000]})
        raise
    finally:
        if context_token is not None:
            request_id_context.reset(context_token)
        connection.close()


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_id: str
    status: str
    result: dict[str, Any]


def _terminate_child(process, *, join_timeout: float = 1.0) -> None:
    """Stop a child before its Run lease can be released to another worker."""
    if not process.is_alive():
        return
    try:
        pid = getattr(process, "pid", None)
        if pid and os.name == "posix":
            os.killpg(pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    process.join(timeout=join_timeout)
    if process.is_alive():
        try:
            pid = getattr(process, "pid", None)
            if pid and os.name == "posix":
                os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.join(timeout=join_timeout)
    if process.is_alive():
        raise RuntimeError("Task child process remained alive after terminate and kill")


class TaskExecutor:
    def __init__(self, *, repository: TaskControlRepository, registry: TaskRegistry, holder_id: str) -> None:
        self.repository = repository
        self.registry = registry
        self.holder_id = holder_id
        self._stop_event = multiprocessing.Event()

    def stop(self) -> None:
        """Request the current execution loop to stop at a safe boundary."""
        self._stop_event.set()

    def execute_one(self) -> ExecutionResult | None:
        if self._stop_event.is_set():
            return None
        run = self.repository.claim_next(holder_id=self.holder_id, lease_seconds=60)
        if run is None:
            return None
        def append_lifecycle_event(event_type: str, *, severity: str = "info", message: str = "", error_code: str = "", metadata: Mapping[str, Any] | None = None) -> None:
            try:
                self.repository.append_event(
                    run_id=run.run_id, holder_id=self.holder_id, event_type=event_type,
                    fencing_token=run.fencing_token,
                    severity=severity, message=message, error_code=error_code,
                    metadata=metadata, external_request_id=run.run_id,
                )
            except Exception:
                logger.warning("Task Event persistence failed for run_id=%s event_type=%s", run.run_id, event_type, exc_info=True)
        def finalize_linked_work(reason: str) -> None:
            """Best-effort domain cleanup before a non-retryable Run failure."""
            if definition.cancellation_handler is None:
                return
            try:
                definition.cancellation_handler(run.parameters)
            except Exception as exc:
                append_lifecycle_event(
                    "domain_finalization_failed", severity="error",
                    error_code="domain_finalization_failed",
                    message=f"{reason}: {str(exc)[:1500]}",
                )
        try:
            definition: TaskDefinition = self.registry.get(run.task_key)
        except KeyError:
            append_lifecycle_event(
                "task_failed", severity="error", error_code="definition_version_unavailable",
                message=f"Task Definition is not registered: {run.task_key}",
            )
            self.repository.transition_run(
                run.run_id,
                "failed",
                error_code="definition_version_unavailable",
                error_summary=f"Task Definition is not registered: {run.task_key}",
                holder_id=self.holder_id, fencing_token=run.fencing_token,
            )
            self.repository.release_lease(run_id=run.run_id, holder_id=self.holder_id, fencing_token=run.fencing_token)
            return ExecutionResult(run.run_id, "failed", {})
        if definition.definition_version != run.definition_version:
            append_lifecycle_event(
                "task_failed", severity="error", error_code="definition_version_unavailable",
                message=f"Task Definition version {run.definition_version} is unavailable",
            )
            self.repository.transition_run(
                run.run_id,
                "failed",
                error_code="definition_version_unavailable",
                error_summary=(
                    f"Task Definition version {run.definition_version} is unavailable; "
                    f"current version is {definition.definition_version}"
                ),
                holder_id=self.holder_id, fencing_token=run.fencing_token,
            )
            self.repository.release_lease(run_id=run.run_id, holder_id=self.holder_id, fencing_token=run.fencing_token)
            return ExecutionResult(run.run_id, "failed", {})
        parent, child = multiprocessing.Pipe(duplex=False)
        context = multiprocessing.get_context("spawn")
        cancel_event = context.Event()
        process = context.Process(
            target=_run_child,
            args=(definition.handler, run.parameters, run.run_id, child, cancel_event),
            daemon=False,
        )
        process.start()
        child.close()
        append_lifecycle_event("attempt_started", metadata={"attempt": run.attempt_no, "task_key": run.task_key})
        result: dict[str, Any] = {}
        error: dict[str, Any] | None = None
        last_heartbeat = time.monotonic()
        started_at = last_heartbeat
        next_cancel_check = last_heartbeat
        cancel_requested_at: float | None = None
        forced_interruption = False
        cancel_grace_seconds = max(1, int(definition.capabilities.get("cancel_grace_seconds", 60)))
        no_total_timeout = bool(definition.capabilities.get("no_total_timeout", False))
        timeout_seconds = None if no_total_timeout else definition.capabilities.get("timeout_seconds")
        max_retries = max(0, int(definition.capabilities.get("max_retries", 3)))
        retryable_errors = tuple(definition.capabilities.get("retryable_errors", ()))
        last_progress_write = 0.0
        last_progress_stage = ""
        pending_progress: dict[str, Any] | None = None
        timed_out = False
        try:
            while process.is_alive() or parent.poll(0.05):
                if self._stop_event.is_set() and process.is_alive():
                    _terminate_child(process)
                    forced_interruption = True
                    break
                if parent.poll(0.05):
                    try:
                        message = parent.recv()
                    except EOFError:
                        break
                    if message.get("kind") == "event":
                        append_lifecycle_event(**{key: message.get(key) for key in ("event_type", "message", "severity", "metadata", "error_code")})
                    elif message.get("kind") == "progress":
                        progress = {key: message.get(key) for key in ("stage", "current", "total", "unit", "message", "checkpoint_ref")}
                        now = time.monotonic()
                        if progress.get("stage") != last_progress_stage or now - last_progress_write >= 5:
                            self.repository.update_progress(run_id=run.run_id, holder_id=self.holder_id, fencing_token=run.fencing_token, **progress)
                            last_progress_write = now
                            last_progress_stage = str(progress.get("stage") or "")
                            pending_progress = None
                        else:
                            pending_progress = progress
                    elif message.get("kind") == "domain_reference":
                        self.repository.update_domain_reference(
                            run_id=run.run_id,
                            holder_id=self.holder_id,
                            fencing_token=run.fencing_token,
                            domain_kind=str(message.get("domain_kind") or ""),
                            domain_run_id=str(message.get("domain_run_id") or ""),
                        )
                    elif message.get("kind") == "safe_cancel":
                        self.repository.request_cancel(run.run_id)
                        cancel_event.set()
                        append_lifecycle_event(
                            "task_cancel_requested", severity="warning",
                            error_code=str(message.get("error_code") or "safe_cancel_requested"),
                            message=str(message.get("message") or "Task requested cancellation at a safe boundary"),
                            metadata=message.get("metadata") or {},
                        )
                    elif message.get("kind") == "result":
                        result = dict(message.get("result") or {})
                    elif message.get("kind") == "error":
                        error = message
                now = time.monotonic()
                if timeout_seconds is not None and now - started_at >= float(timeout_seconds):
                    _terminate_child(process)
                    timed_out = True
                    break
                if now >= next_cancel_check and process.is_alive():
                    current = self.repository.get_run(run.run_id)
                    if current is not None and current.status == "cancel_requested":
                        if cancel_requested_at is None:
                            if not bool(definition.capabilities.get("supports_cancel", True)):
                                _terminate_child(process)
                                error = {
                                    "error_code": "cancellation_not_supported",
                                    "message": "Task Definition does not support cancellation",
                                }
                                break
                            cancel_requested_at = now
                            cancel_event.set()
                            if definition.cancellation_handler is not None:
                                try:
                                    definition.cancellation_handler(run.parameters)
                                except Exception as exc:
                                    _terminate_child(process)
                                    error = {
                                        "error_code": "cancellation_handler_failed",
                                        "message": str(exc)[:2000],
                                    }
                                    break
                        elif now - cancel_requested_at >= cancel_grace_seconds:
                            _terminate_child(process)
                            forced_interruption = True
                            break
                    next_cancel_check = now + 1
                if now - last_heartbeat >= 20:
                    if not self.repository.renew_lease(run_id=run.run_id, holder_id=self.holder_id, lease_seconds=60, fencing_token=run.fencing_token):
                        _terminate_child(process)
                        error = {"error_code": "lease_lost", "message": "Task Run lease could not be renewed"}
                    last_heartbeat = now
            process.join(timeout=1)
            if pending_progress is not None and not forced_interruption and not timed_out:
                self.repository.update_progress(run_id=run.run_id, holder_id=self.holder_id, fencing_token=run.fencing_token, **pending_progress)
            if timed_out:
                finalize_linked_work("Task timed out")
                append_lifecycle_event(
                    "task_failed", severity="error", error_code="timed_out",
                    message=f"Task exceeded timeout_seconds={timeout_seconds}",
                )
                self.repository.transition_run(
                    run.run_id,
                    "failed",
                    error_code="timed_out",
                    error_summary=f"Task exceeded timeout_seconds={timeout_seconds}",
                    holder_id=self.holder_id, fencing_token=run.fencing_token,
                )
                return ExecutionResult(run.run_id, "failed", {})
            if forced_interruption:
                finalize_linked_work("Task was forcibly interrupted")
                append_lifecycle_event(
                    "task_failed", severity="error", error_code="forced_interruption",
                    message=f"Task did not stop within {cancel_grace_seconds} seconds",
                )
                self.repository.transition_run(
                    run.run_id,
                    "failed",
                    error_code="forced_interruption",
                    error_summary=f"Task did not stop within {cancel_grace_seconds} seconds",
                    holder_id=self.holder_id, fencing_token=run.fencing_token,
                )
                return ExecutionResult(run.run_id, "failed", {})
            if error or process.exitcode:
                message = error or {"error_code": "child_process_exit", "message": f"exit code {process.exitcode}"}
                error_code = str(message.get("error_code") or "task_failed")
                error_summary = str(message.get("message") or "")[:2000]
                if is_temporary_error(error_code, retryable_errors) and run.attempt_no <= max_retries:
                    delay_seconds = retry_delay_seconds(run.attempt_no)
                    append_lifecycle_event(
                        "retry_scheduled", severity="warning", error_code=error_code,
                        message=error_summary, metadata={"attempt": run.attempt_no, "delay_seconds": delay_seconds},
                    )
                    self.repository.schedule_retry(
                        run.run_id,
                        error_code=error_code,
                        error_summary=error_summary,
                        delay_seconds=delay_seconds,
                        holder_id=self.holder_id, fencing_token=run.fencing_token,
                    )
                    return ExecutionResult(run.run_id, "retry_wait", {})
                finalize_linked_work(f"Task failed with {error_code}")
                append_lifecycle_event("task_failed", severity="error", error_code=error_code, message=error_summary)
                self.repository.transition_run(run.run_id, "failed", error_code=error_code, error_summary=error_summary, holder_id=self.holder_id, fencing_token=run.fencing_token)
                return ExecutionResult(run.run_id, "failed", {})
            current = self.repository.get_run(run.run_id)
            if current is not None and current.status == "cancel_requested":
                append_lifecycle_event("task_cancelled", severity="warning", message="Task stopped at a safe boundary")
                self.repository.transition_run(run.run_id, "cancelled", result_status="cancelled", result_summary=result, holder_id=self.holder_id, fencing_token=run.fencing_token)
                return ExecutionResult(run.run_id, "cancelled", result)
            append_lifecycle_event("task_succeeded", metadata={"attempt": run.attempt_no})
            self.repository.transition_run(run.run_id, "succeeded", result_status="succeeded", result_summary=result, holder_id=self.holder_id, fencing_token=run.fencing_token)
            return ExecutionResult(run.run_id, "succeeded", result)
        finally:
            parent.close()
            _terminate_child(process)
            self.repository.release_lease(run_id=run.run_id, holder_id=self.holder_id, fencing_token=run.fencing_token)
