#!/usr/bin/env python3
"""One-time operational hard cutover from the retired finite-task stack.

This script is deliberately outside the application runtime.  It talks to the
legacy containers only through ``docker exec``, records interrupted domain work,
and never dispatches or executes a legacy task.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WORKER_CONTAINER = "quantdinger-celery-worker"
BEAT_CONTAINER = "quantdinger-celery-beat"
REDIS_CONTAINER = "quantdinger-redis-jobs"
QUEUES = ("jobs", "ai", "maintenance", "celery")
REQUIRED_GATES = (
    "all_task_definitions_migrated", "cron_and_missed_skip", "exclusivity_and_fifo",
    "automatic_and_manual_retry", "safe_cancel_and_forced_interruption", "worker_loss",
    "progress_events_and_retention", "authorization_and_audit", "frontend_console",
    "hard_cutover_drill", "targeted_backend_tests", "targeted_frontend_tests",
    "compose_validation",
)


@dataclass(frozen=True, slots=True)
class LegacyTask:
    task_id: str
    task_name: str
    args: list[Any]
    kwargs: dict[str, Any]
    source: str
    queue: str = ""


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def _container_running(name: str) -> bool:
    result = _run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _parse_json_output(output: str) -> Any:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        return {}
    return json.loads(output[start : end + 1])


def _normalize_inspect(payload: Any, source: str) -> list[LegacyTask]:
    tasks: list[LegacyTask] = []
    for worker_rows in (payload or {}).values():
        for raw in worker_rows or []:
            row = raw.get("request") if source == "scheduled" and isinstance(raw, dict) else raw
            row = row or {}
            args = row.get("args") or []
            kwargs = row.get("kwargs") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = [args]
            if isinstance(kwargs, str):
                try:
                    kwargs = json.loads(kwargs)
                except json.JSONDecodeError:
                    kwargs = {}
            tasks.append(LegacyTask(
                task_id=str(row.get("id") or row.get("uuid") or ""),
                task_name=str(row.get("name") or row.get("task") or ""),
                args=list(args) if isinstance(args, (list, tuple)) else [],
                kwargs=dict(kwargs) if isinstance(kwargs, dict) else {},
                source=source,
                queue=str((row.get("delivery_info") or {}).get("routing_key") or ""),
            ))
    return tasks


def inspect_worker(*, require_running: bool = True) -> list[LegacyTask]:
    if not _container_running(WORKER_CONTAINER):
        if require_running:
            raise RuntimeError("legacy worker container is unavailable; refusing fail-open cutover")
        return []
    tasks: list[LegacyTask] = []
    for source in ("active", "reserved", "scheduled"):
        result = _run([
            "docker", "exec", WORKER_CONTAINER, "celery", "-A",
            "app.celery_app:celery_app", "inspect", source, "--json", "--timeout", "5",
        ], check=False)
        if result.returncode != 0:
            raise RuntimeError(f"legacy worker inspect {source} failed: {result.stderr.strip()}")
        tasks.extend(_normalize_inspect(_parse_json_output(result.stdout), source))
    return tasks


def decode_queue_message(raw: str, queue: str) -> LegacyTask | None:
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            return None
        headers = envelope.get("headers") or {}
        if not isinstance(headers, dict):
            return None
        task_id = str(headers.get("id") or "").strip()
        task_name = str(headers.get("task") or "").strip()
        if not task_id or not task_name:
            return None
        body = json.loads(base64.b64decode(envelope.get("body") or "").decode("utf-8"))
        if not isinstance(body, (list, tuple)) or len(body) < 2:
            return None
        args, kwargs = body[0], body[1]
        if not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
            return None
        return LegacyTask(
            task_id=task_id, task_name=task_name,
            args=list(args), kwargs=dict(kwargs), source="queued", queue=queue,
        )
    except Exception:
        return None


def inspect_queues() -> tuple[list[LegacyTask], dict[str, int], dict[str, int]]:
    if not _container_running(REDIS_CONTAINER):
        raise RuntimeError("legacy Redis container is unavailable; refusing fail-open cutover")
    tasks: list[LegacyTask] = []
    lengths: dict[str, int] = {}
    undecodable: dict[str, int] = {}
    shell = (
        'if [ -n "$REDIS_PASSWORD" ]; then '
        'exec redis-cli --no-auth-warning -a "$REDIS_PASSWORD" --raw "$@"; '
        'else exec redis-cli --raw "$@"; fi'
    )
    for queue in QUEUES:
        result = _run(["docker", "exec", REDIS_CONTAINER, "sh", "-c", shell, "redis-cli", "LRANGE", queue, "0", "-1"], check=False)
        if result.returncode != 0:
            raise RuntimeError(f"queue inspection failed for {queue}: {result.stderr.strip()}")
        messages = result.stdout.splitlines()
        lengths[queue] = len(messages)
        undecodable[queue] = 0
        for message in messages:
            decoded = decode_queue_message(message, queue)
            if decoded is not None:
                tasks.append(decoded)
            else:
                undecodable[queue] += 1
    return tasks, lengths, undecodable


def snapshot(*, require_worker: bool = True) -> dict[str, Any]:
    inspected = inspect_worker(require_running=require_worker)
    queued, lengths, undecodable = inspect_queues()
    by_id: dict[tuple[str, str], LegacyTask] = {}
    for task in [*inspected, *queued]:
        by_id[(task.task_id, task.source)] = task
    return {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "workerContainerRunning": _container_running(WORKER_CONTAINER),
        "beatContainerRunning": _container_running(BEAT_CONTAINER),
        "queueContainerRunning": _container_running(REDIS_CONTAINER),
        "queueLengths": lengths,
        "undecodableQueueMessages": undecodable,
        "tasks": [asdict(task) for task in by_id.values()],
    }


def _database_connection(database_url: str):
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:
        raise RuntimeError("psycopg2 is required to record migration results") from exc
    return psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)


def _domain_reference(task: LegacyTask) -> tuple[str, str]:
    first = str(task.args[0]) if task.args else ""
    mapping = {
        "quantdinger.tasks.cn_market_history_sync": ("cn_market_history", first),
        "quantdinger.tasks.cn_fundamental_run": ("cn_fundamental", first),
        "quantdinger.tasks.agent_job": ("agent_job", first),
        "quantdinger.tasks.fast_analysis": ("fast_analysis", first),
    }
    return mapping.get(task.task_name, ("", ""))


def record_migration(tasks: Iterable[LegacyTask], *, database_url: str) -> None:
    with _database_connection(database_url) as db:
        with db.cursor() as cur:
            for task in tasks:
                code = "migration_interrupted" if task.source == "active" else "migration_skipped"
                domain_kind, domain_id = _domain_reference(task)
                if domain_kind == "cn_market_history" and domain_id:
                    cur.execute(
                        """UPDATE qd_cn_history_sync_runs SET status='failed', last_error_code=%s,
                               last_error=%s, finished_at=NOW(), updated_at=NOW() WHERE run_id=%s""",
                        (code, "Finite-task hard cutover; inspect the last domain checkpoint before manual retry", domain_id),
                    )
                elif domain_kind == "cn_fundamental" and domain_id:
                    cur.execute(
                        """UPDATE qd_cn_fundamental_sync_runs SET status='failed', last_error=%s,
                               finished_at=NOW(), updated_at=NOW() WHERE run_id=%s""",
                        (code, domain_id),
                    )
                elif domain_kind == "agent_job" and domain_id:
                    cur.execute(
                        """UPDATE qd_agent_jobs SET status='failed', error=%s,
                               finished_at=NOW() WHERE job_id=%s""",
                        (code, domain_id),
                    )
                elif domain_kind == "fast_analysis" and domain_id.isdigit():
                    cur.execute(
                        """UPDATE qd_analysis_memory SET task_status='failed', task_error=%s,
                               summary=%s, updated_at=NOW() WHERE id=%s""",
                        (code, "Analysis interrupted by finite-task hard cutover", int(domain_id)),
                    )
                cur.execute(
                    """INSERT INTO qd_task_audit
                           (actor_user_id, action, target_type, target_id, reason, before_summary, after_summary)
                       VALUES (NULL, %s, 'legacy_task', %s, %s, %s::jsonb, '{}'::jsonb)""",
                    (
                        code, task.task_id or f"{task.task_name}:{domain_id}",
                        "One-time finite-task hard cutover; automatic internal retry is prohibited",
                        json.dumps({
                            "taskName": task.task_name, "source": task.source, "queue": task.queue,
                            "domainKind": domain_kind, "domainRunId": domain_id,
                        }),
                    ),
                )
        db.commit()


def _revoke(task: LegacyTask, *, terminate: bool) -> None:
    if not task.task_id or not _container_running(WORKER_CONTAINER):
        return
    command = [
        "docker", "exec", WORKER_CONTAINER, "celery", "-A", "app.celery_app:celery_app",
        "control", "revoke", task.task_id,
    ]
    if terminate:
        command.extend(["--terminate", "--signal=SIGTERM"])
    _run(command, check=False)


def _purge_queues() -> None:
    if not _container_running(REDIS_CONTAINER):
        return
    shell = (
        'if [ -n "$REDIS_PASSWORD" ]; then '
        'exec redis-cli --no-auth-warning -a "$REDIS_PASSWORD" DEL "$@"; '
        'else exec redis-cli DEL "$@"; fi'
    )
    _run(["docker", "exec", REDIS_CONTAINER, "sh", "-c", shell, "redis-cli", *QUEUES])


def _image_identity(container: str) -> dict[str, str]:
    result = _run(["docker", "inspect", "-f", "{{.Config.Image}}|{{.Image}}", container], check=False)
    if result.returncode != 0:
        return {"image": "not-running", "imageId": ""}
    image, _, image_id = result.stdout.strip().partition("|")
    return {"image": image, "imageId": image_id}


def recovery_manifest(*, database_backup_ref: str, before: dict[str, Any], output: Path) -> dict[str, Any]:
    commit = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    dirty = bool(_run(["git", "status", "--porcelain"]).stdout.strip())
    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "gitCommitBeforeCutover": commit,
        "workingTreeDirty": dirty,
        "images": {
            "backend": _image_identity("quantdinger-backend"),
            "legacyWorker": _image_identity(WORKER_CONTAINER),
            "legacyBeat": _image_identity(BEAT_CONTAINER),
        },
        "databaseBackupRef": database_backup_ref,
        "databaseRecovery": "Restore the referenced database backup together with the recorded complete legacy images and Compose file; no runtime fallback exists.",
        "legacySnapshot": before,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def validate_gates(evidence_path: Path, state: dict[str, Any]) -> list[str]:
    evidence = json.loads(evidence_path.read_text())
    failures = [gate for gate in REQUIRED_GATES if evidence.get(gate) is not True]
    if any(int(value) for value in state.get("queueLengths", {}).values()):
        failures.append("legacy_queues_empty")
    if state.get("tasks"):
        failures.append("legacy_active_reserved_scheduled_empty")
    if any(int(value) for value in state.get("undecodableQueueMessages", {}).values()):
        failures.append("legacy_queue_messages_undecodable")
    return failures


def command_inspect(args) -> int:
    state = snapshot()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    has_work = bool(state["tasks"]) or any(int(value) for value in state["queueLengths"].values())
    return 2 if has_work else 0


def command_cutover(args) -> int:
    if args.confirm != "TERMINATE_LEGACY_TASKS":
        raise RuntimeError("cutover requires --confirm TERMINATE_LEGACY_TASKS")
    if not _container_running(BEAT_CONTAINER) or not _container_running(WORKER_CONTAINER) or not _container_running(REDIS_CONTAINER):
        raise RuntimeError("all legacy containers must be running for a quiescent cutover")
    # Stop new producers before taking the authoritative snapshot.
    _run(["docker", "stop", "-t", "10", BEAT_CONTAINER])
    before = snapshot()
    if any(int(value) for value in before.get("undecodableQueueMessages", {}).values()):
        raise RuntimeError(
            "legacy_queue_messages_undecodable: inspect and preserve malformed queue messages before cutover"
        )
    recovery_manifest(
        database_backup_ref=args.database_backup_ref,
        before=before,
        output=Path(args.manifest),
    )
    tasks = [LegacyTask(**item) for item in before["tasks"]]
    for task in tasks:
        _revoke(task, terminate=task.source == "active")
    _purge_queues()
    if _container_running(WORKER_CONTAINER):
        _run(["docker", "stop", "-t", "10", WORKER_CONTAINER])
    after = snapshot(require_worker=False)
    if after["tasks"] or any(int(value) for value in after["queueLengths"].values()):
        raise RuntimeError("legacy work remains after cutover; deployment is blocked")
    record_migration(tasks, database_url=args.database_url)
    print(json.dumps({"status": "cutover_complete", "before": before, "after": after}, ensure_ascii=False, indent=2))
    return 0


def command_gate(args) -> int:
    state = snapshot()
    failures = validate_gates(Path(args.evidence), state)
    report = {"status": "passed" if not failures else "blocked", "failures": failures, "state": state}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="List work and fail when any legacy task remains")
    inspect_parser.set_defaults(handler=command_inspect)
    cutover = sub.add_parser("cutover", help="Record and terminate legacy work once")
    cutover.add_argument("--confirm", required=True)
    cutover.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""), required=not bool(os.getenv("DATABASE_URL")))
    cutover.add_argument("--database-backup-ref", required=True)
    cutover.add_argument("--manifest", default="artifacts/internal-task-hard-cutover/recovery-manifest.json")
    cutover.set_defaults(handler=command_cutover)
    gate = sub.add_parser("gate", help="Validate all hard-gate evidence and an empty legacy runtime")
    gate.add_argument("--evidence", required=True)
    gate.set_defaults(handler=command_gate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
