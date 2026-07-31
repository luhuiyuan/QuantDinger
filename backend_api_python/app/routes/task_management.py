"""Task Management APIs for internal finite Task Runs."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.task_control.builtin_tasks import register_phase_one_tasks, register_phase_two_tasks
from app.services.task_control.cron import CronExpression
from app.services.task_control.registry import default_task_registry
from app.services.task_control.repository import MANUAL_RETRY_STATUSES, TaskControlRepository
from app.utils.auth import admin_required, login_required


task_management_blp = Blueprint("task_management", __name__)


def _repository() -> TaskControlRepository:
    return TaskControlRepository()


def _registry():
    register_phase_one_tasks(default_task_registry)
    register_phase_two_tasks(default_task_registry)
    return default_task_registry


def _success(data=None, status=200):
    return jsonify({"code": 1, "msg": "success", "data": data}), status


def _error(message: str, status: int, *, details=None):
    return jsonify({"code": 0, "msg": message, "data": details}), status


def _integer_arg(name: str, default: int, *, minimum: int = 0, maximum: int = 500) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _is_admin() -> bool:
    return getattr(g, "user_role", "") == "admin"


def _reason(payload: dict) -> str:
    return str(payload.get("reason") or "")[:1000]


def _boolean(value, *, field: str = "enabled") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


@task_management_blp.route("/overview", methods=["GET"])
@login_required
def overview():
    owner = None if _is_admin() else int(g.user_id)
    return _success(_repository().overview(owner_user_id=owner))


@task_management_blp.route("/definitions", methods=["GET"])
@login_required
@admin_required
def definitions():
    return _success(_repository().list_definitions())


@task_management_blp.route("/schedules", methods=["GET", "POST"])
@login_required
@admin_required
def schedules():
    repo = _repository()
    try:
        if request.method == "GET":
            enabled_raw = request.args.get("enabled")
            enabled = None if enabled_raw in (None, "") else _boolean(enabled_raw)
            limit = _integer_arg("limit", 100, minimum=1)
            offset = _integer_arg("offset", 0, maximum=1_000_000)
            rows = repo.list_schedules(
                task_key=str(request.args.get("taskKey") or ""),
                enabled=enabled,
                limit=limit,
                offset=offset,
            )
            return _success({"items": rows, "limit": limit, "offset": offset})

        payload = request.get_json(silent=True) or {}
        definition = _registry().get(str(payload.get("taskKey") or ""))
        parameters = definition.validate_parameters(payload.get("parameters") or {})
        cron = str(payload.get("cron") or "").strip()
        timezone_name = str(payload.get("timezone") or "Asia/Shanghai").strip()
        enabled = _boolean(payload.get("enabled", True))
        CronExpression.parse(cron).next_after(datetime.now(timezone.utc), timezone_name)
        schedule_id = repo.create_schedule(
            task_key=definition.task_key,
            cron_expression=cron,
            timezone_name=timezone_name,
            enabled=enabled,
            parameters=parameters,
            actor_user_id=int(g.user_id),
            audit={"actor_user_id": int(g.user_id), "action": "create_schedule", "target_type": "schedule", "target_id": "", "reason": _reason(payload),
                   "after": {"taskKey": definition.task_key, "cron": cron, "timezone": timezone_name, "parameters": parameters, "enabled": enabled}},
        )
        if hasattr(repo, "audit"):
            repo.write_audit(actor_user_id=int(g.user_id), action="create_schedule", target_type="schedule", target_id=str(schedule_id), reason=_reason(payload), after={"taskKey": definition.task_key, "cron": cron, "timezone": timezone_name, "parameters": parameters, "enabled": enabled})
        return _success({"scheduleId": schedule_id}, 201)
    except (KeyError, ValueError) as exc:
        return _error(str(exc), 400)


@task_management_blp.route("/schedules/<int:schedule_id>", methods=["PUT"])
@login_required
@admin_required
def revise_schedule(schedule_id: int):
    payload = request.get_json(silent=True) or {}
    repo = _repository()
    existing = repo.get_schedule(schedule_id)
    if existing is None:
        return _error("not_found", 404)
    try:
        definition = _registry().get(str(existing["task_key"]))
        supplied_parameters = payload["parameters"] if "parameters" in payload else existing.get("parameters") or {}
        parameters = definition.validate_parameters(supplied_parameters)
        cron = str(payload.get("cron", existing.get("cron_expression") or "")).strip()
        timezone_name = str(payload.get("timezone", existing.get("timezone") or "Asia/Shanghai")).strip()
        enabled = _boolean(payload.get("enabled", existing.get("enabled", True)))
        CronExpression.parse(cron).next_after(datetime.now(timezone.utc), timezone_name)
        action = "pause_schedule" if existing.get("enabled") and not enabled else "revise_schedule"
        revision = repo.revise_schedule(
            schedule_id, cron_expression=cron, timezone_name=timezone_name,
            enabled=enabled, parameters=parameters, updated_by=int(g.user_id),
            audit={"actor_user_id": int(g.user_id), "action": action, "target_type": "schedule", "target_id": str(schedule_id), "reason": _reason(payload), "before": existing, "after": {"cron": cron, "timezone": timezone_name, "enabled": enabled, "parameters": parameters}},
        )
        after = {"cron": cron, "timezone": timezone_name, "enabled": enabled, "parameters": parameters}
        if hasattr(repo, "audit"):
            repo.write_audit(actor_user_id=int(g.user_id), action=action, target_type="schedule", target_id=str(schedule_id), reason=_reason(payload), before=existing, after=after)
        return _success({"scheduleId": schedule_id, "revision": revision})
    except (KeyError, ValueError) as exc:
        return _error(str(exc), 400)


@task_management_blp.route("/runs", methods=["GET", "POST"])
@login_required
def runs():
    repo = _repository()
    try:
        if request.method == "GET":
            limit = _integer_arg("limit", 100, minimum=1)
            offset = _integer_arg("offset", 0, maximum=1_000_000)
            requested_owner = request.args.get("ownerUserId")
            owner = None if _is_admin() else int(g.user_id)
            if _is_admin() and requested_owner not in (None, ""):
                owner = int(requested_owner)
            filters = {
                "owner_user_id": owner,
                "status": str(request.args.get("status") or ""),
                "task_key": str(request.args.get("taskKey") or ""),
                "error_code": str(request.args.get("errorCode") or ""),
                "created_from": str(request.args.get("createdFrom") or ""),
                "created_to": str(request.args.get("createdTo") or ""),
            }
            rows = repo.list_runs(**filters, limit=limit, offset=offset)
            total = repo.count_runs(**filters)
            return _success({"items": rows, "limit": limit, "offset": offset, "total": total})

        if not _is_admin():
            return _error("admin_required", 403)
        payload = request.get_json(silent=True) or {}
        definition = _registry().get(str(payload.get("taskKey") or ""))
        parameters = definition.validate_parameters(payload.get("parameters") or {})
        run, created = repo.create_run(
            task_key=definition.task_key,
            definition_version=definition.definition_version,
            exclusivity_key=definition.exclusivity_key(parameters, int(g.user_id)),
            parameters=parameters,
            priority=definition.priority,
            owner_user_id=int(g.user_id),
            domain_kind=str(payload.get("domainKind") or ""),
            domain_run_id=str(payload.get("domainRunId") or ""),
            audit={"actor_user_id": int(g.user_id), "action": "start_run", "target_type": "run", "target_id": "", "reason": _reason(payload), "after": {"taskKey": definition.task_key, "parameters": parameters}},
        )
        if hasattr(repo, "audit"):
            repo.write_audit(
            actor_user_id=int(g.user_id), action="start_run", target_type="run",
            target_id=run.run_id, reason=_reason(payload),
            after={"created": created, "taskKey": definition.task_key, "parameters": parameters},
        )
        return _success({"runId": run.run_id, "created": created}, 201 if created else 200)
    except (KeyError, ValueError) as exc:
        return _error(str(exc), 400)


@task_management_blp.route("/runs/<string:run_id>", methods=["GET"])
@login_required
def run_detail(run_id: str):
    repo = _repository()
    run = repo.get_run(run_id)
    if run is None or (not _is_admin() and run.owner_user_id != int(g.user_id)):
        return _error("not_found", 404)
    detail = repo.get_run_detail(run_id)
    return _success(detail)


@task_management_blp.route("/runs/<string:run_id>/cancel", methods=["POST"])
@login_required
@admin_required
def cancel_run(run_id: str):
    repo = _repository()
    original = repo.get_run(run_id)
    if original is None:
        return _error("not_found", 404)
    payload = request.get_json(silent=True) or {}
    try:
        definition = _registry().get(original.task_key)
        if not definition.capabilities.get("supports_cancel", True):
            return _error("cancellation_not_supported", 409)
        run = repo.request_cancel(run_id, audit={"actor_user_id": int(g.user_id), "action": "cancel_run", "target_type": "run", "target_id": run_id, "reason": _reason(payload), "before": {"status": original.status}})
        # Finalize domain state immediately for queued runs; running children
        # will invoke the same idempotent handler at their cancellation fence.
        if definition.cancellation_handler is not None and original.status in {"queued", "retry_wait", "cancel_requested"}:
            definition.cancellation_handler(original.parameters)
        if hasattr(repo, "audit"):
            repo.write_audit(
            actor_user_id=int(g.user_id), action="cancel_run", target_type="run",
            target_id=run_id, reason=_reason(payload), before={"status": original.status}, after={"status": run.status},
        )
        return _success({"runId": run.run_id, "status": run.status})
    except (KeyError, ValueError) as exc:
        return _error(str(exc), 409)


@task_management_blp.route("/runs/<string:run_id>/retry", methods=["POST"])
@login_required
@admin_required
def retry_run(run_id: str):
    payload = request.get_json(silent=True) or {}
    repo = _repository()
    original = repo.get_run(run_id)
    if original is None:
        return _error("not_found", 404)
    if original.status not in MANUAL_RETRY_STATUSES:
        return _error("only_failed_or_cancelled_runs_can_be_retried", 409)
    try:
        definition = _registry().get(original.task_key)
        definition.validate_parameters(original.parameters)
        checkpoint_ref = str(payload.get("checkpointRef") or "")
        if checkpoint_ref and checkpoint_ref != original.checkpoint_ref:
            return _error("checkpoint_ref does not match the original Task Run", 409)
        if definition.manual_retry_handler is not None:
            def materialize_domain():
                parameters = definition.validate_parameters(
                    definition.manual_retry_handler(original.parameters, int(g.user_id))
                )
                return parameters, str(parameters.get("run_id") or "")

            retried, created = repo.create_run_with_domain_factory(
                task_key=definition.task_key,
                definition_version=definition.definition_version,
                exclusivity_key=original.exclusivity_key,
                domain_kind=original.domain_kind,
                domain_factory=materialize_domain,
                priority=original.priority,
                owner_user_id=original.owner_user_id,
                retry_of_run_id=original.run_id,
                compensate=(
                    (lambda domain_run_id: definition.cancellation_handler({"run_id": domain_run_id}))
                    if definition.cancellation_handler is not None else None
                ),
            )
            if not created:
                return _error("An active Task Run already owns the exclusivity key", 409)
        else:
            retried = repo.create_manual_retry(
                run_id, definition_version=definition.definition_version,
                requested_by=int(g.user_id), checkpoint_ref=checkpoint_ref,
                parameters=original.parameters, domain_run_id=original.domain_run_id,
                exclusivity_key=definition.exclusivity_key(original.parameters, original.owner_user_id),
            )
        repo.write_audit(
            actor_user_id=int(g.user_id), action="retry_run", target_type="run",
            target_id=retried.run_id, reason=_reason(payload),
            before={"runId": run_id, "status": original.status, "checkpointRef": original.checkpoint_ref},
            after={"retryOfRunId": run_id, "checkpointRef": payload.get("checkpointRef") or ""},
        )
        return _success({"runId": retried.run_id, "retryOfRunId": run_id}, 201)
    except (KeyError, ValueError) as exc:
        return _error(str(exc), 409)


@task_management_blp.route("/logs", methods=["GET"])
@login_required
def logs():
    repo = _repository()
    try:
        run_id = str(request.args.get("runId") or "")
        owner = None if _is_admin() else int(g.user_id)
        if run_id and not _is_admin():
            run = repo.get_run(run_id)
            if run is None or run.owner_user_id != int(g.user_id):
                return _error("not_found", 404)
        if _is_admin() and request.args.get("ownerUserId") not in (None, ""):
            owner = int(request.args["ownerUserId"])
        cursor = _integer_arg("cursor", 0, maximum=9_223_372_036_854_775_807)
        limit = _integer_arg("limit", 100, minimum=1)
        rows = repo.list_events(
            run_id=run_id,
            event_type=str(request.args.get("eventType") or ""),
            error_code=str(request.args.get("errorCode") or ""),
            task_key=str(request.args.get("taskKey") or ""),
            run_status=str(request.args.get("status") or ""),
            owner_user_id=owner,
            occurred_from=str(request.args.get("occurredFrom") or ""),
            occurred_to=str(request.args.get("occurredTo") or ""),
            external_request_id=str(request.args.get("externalRequestId") or ""),
            provider=str(request.args.get("provider") or ""),
            cursor=cursor,
            limit=limit,
            tail=str(request.args.get("tail") or "").strip().lower() in {"1", "true", "yes", "on"},
        )
        next_cursor = rows[-1]["id"] if rows else cursor
        return _success({"items": rows, "nextCursor": next_cursor, "limit": limit})
    except ValueError as exc:
        return _error(str(exc), 400)
