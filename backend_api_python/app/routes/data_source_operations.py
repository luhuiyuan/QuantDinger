"""Fine-grained administrator API for Data Source Operations."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.data_routing.bootstrap import load_default_data_routing_registry
from app.services.data_routing.credential_repository import PostgresProviderCredentialRepository
from app.services.data_routing.credentials import ProviderCredentialService
from app.services.data_routing.diagnostics import (
    PostgresDiagnosticRepository, ProviderCapabilityTestService, ProviderDiagnosticError,
)
from app.services.data_routing.errors import DataRoutingError
from app.services.data_routing.health import PostgresHealthRepository, ProviderHealthManager
from app.services.data_routing.management_repository import PostgresDataSourceManagementRepository
from app.services.data_routing.policy import RoutingPolicyService
from app.services.data_routing.policy_repository import PostgresRoutingPolicyRepository
from app.services.data_routing.provider_instances import PostgresProviderInstanceRepository, ProviderInstanceService
from app.services.data_routing.quota import PostgresQuotaRepository, QuotaManager
from app.services.data_routing.router_repository import PostgresRouterSecretResolver
from app.services.data_routing.security import StepUpRequiredError, StepUpService
from app.services.data_routing.snapshot import (
    PostgresRoutingSnapshotRepository, RoutingSnapshotEntry, RoutingSnapshotManager, SecretHandle,
)
from app.services.data_routing.cutover import DataRoutingCutoverService, PostgresCutoverRepository
from app.services.data_routing.legacy_import import (
    LegacyCredentialDetector, LegacyCredentialImportService, PostgresLegacyImportRepository,
)
from app.services.data_routing.preflight import CutoverEvidenceCollector, PostgresPreflightEvidenceRepository
from app.utils.auth import login_required, permission_required
from app.utils.credential_crypto import load_provider_credential_keyring


data_source_operations_blp = Blueprint("data_source_operations", __name__)


def _ok(data=None, status=200):
    return jsonify({"code": 1, "msg": "success", "data": data}), status


def _error(exc, status=400):
    details = getattr(exc, "details", {})
    return jsonify({"code": 0, "msg": str(exc), "data": {"error_code": getattr(exc, "code", "invalid_request"), "details": details}}), status


def _payload():
    value = request.get_json(silent=True) or {}
    if not isinstance(value, dict):
        raise ValueError("Request body must be an object")
    return value


def _reason(payload):
    value = str(payload.get("reason") or "").strip()
    if not value:
        raise ValueError("Management change reason is required")
    return value


def _require_step_up():
    StepUpService().require(int(g.user_id), str(request.headers.get("X-Step-Up-Proof") or ""))


def _snapshot_manager():
    manager = RoutingSnapshotManager(PostgresRoutingSnapshotRepository())
    manager.refresh_if_changed(force=True)
    return manager


def _direct_diagnostic_entry(instance_id: int, capability_key: str) -> RoutingSnapshotEntry:
    """Build a safe, ephemeral entry for testing an instance outside a route.

    A routing revision decides which eligible instances serve production traffic.
    It must not decide whether an administrator can test a configured instance
    while investigating or preparing it for that revision.
    """
    credentials = PostgresProviderCredentialRepository()
    context = credentials.get_instance_context(instance_id)
    if context is None:
        raise ProviderDiagnosticError("Provider Instance was not found", details={"instance_id": instance_id})
    if context.lifecycle_status == "retired":
        raise ProviderDiagnosticError("Retired Provider Instance cannot be diagnosed", details={"instance_id": instance_id})

    adapter = load_default_data_routing_registry().adapters.get(context.adapter_key)
    if adapter is None:
        raise ProviderDiagnosticError("Provider Adapter is not registered", details={"adapter_key": context.adapter_key})
    if capability_key not in adapter.capabilities:
        raise ProviderDiagnosticError(
            "Provider Instance does not declare this Data Capability",
            details={"instance_id": instance_id, "capability_key": capability_key},
        )
    credential = credentials.get_active_credential(instance_id)
    if credential is None:
        raise ProviderDiagnosticError(
            "Provider Instance has no active credential to run a diagnostic",
            details={"instance_id": instance_id},
        )

    detail = PostgresDataSourceManagementRepository().get_instance(instance_id)
    if detail is None:
        raise ProviderDiagnosticError("Provider Instance was not found", details={"instance_id": instance_id})
    eligibility_status = next(
        (str(item.get("eligibility_status") or "unverified") for item in detail.get("capabilities", [])
         if item.get("capability_key") == capability_key),
        "unverified",
    )
    return RoutingSnapshotEntry(
        position=1,
        instance_id=instance_id,
        instance_key=str(detail["instance_key"]),
        adapter_key=context.adapter_key,
        display_name=str(detail["display_name"]),
        lifecycle_status=context.lifecycle_status,
        eligibility_status=eligibility_status,
        non_secret_config=context.non_secret_config,
        eligibility_requirements={},
        stricter_quality_profile={},
        secret_handle=SecretHandle(credential.credential_id),
    )


def _diagnostic_entry(instance_id: int, capability_key: str) -> RoutingSnapshotEntry:
    """Use the routed entry when present, otherwise diagnose the instance directly."""
    try:
        pinned = _snapshot_manager().pin(capability_key)
        entry = next((item for item in pinned.entries if item.instance_id == instance_id), None)
        if entry is not None:
            return entry
    except DataRoutingError:
        # Direct diagnostics remain available when no effective route exists.
        pass
    return _direct_diagnostic_entry(instance_id, capability_key)


@data_source_operations_blp.route("/step-up", methods=["POST"])
@login_required
@permission_required("data_sources:view")
def issue_step_up():
    try:
        payload = _payload()
        proof = StepUpService().issue(
            int(g.user_id), password=str(payload.get("password") or ""),
            mfa_code=str(payload.get("mfaCode") or ""),
        )
        return _ok({"proof": proof.token, "expiresAt": proof.expires_at, "method": proof.verification_method})
    except StepUpRequiredError as exc:
        return _error(exc, 401)


@data_source_operations_blp.route("/overview", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def overview():
    try:
        data = PostgresDataSourceManagementRepository().overview()
        try:
            data["readiness"] = dict(_snapshot_manager().readiness())
        except DataRoutingError as exc:
            data["readiness"] = {"ready": False, "reason": exc.code}
        return _ok(data)
    except Exception as exc:
        return _error(exc, 503)


@data_source_operations_blp.route("/readiness", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def readiness():
    try:
        return _ok(dict(_snapshot_manager().readiness()))
    except DataRoutingError as exc:
        return _error(exc, 503)


@data_source_operations_blp.route("/instances", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def list_instances():
    try:
        return _ok(PostgresDataSourceManagementRepository().list_instances(
            limit=request.args.get("limit", 100), offset=request.args.get("offset", 0)
        ))
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/instances", methods=["POST"])
@login_required
@permission_required("data_sources:instances")
def create_instance():
    try:
        payload = _payload()
        adapter_key = str(payload.get("adapterKey") or "")
        adapter = load_default_data_routing_registry().adapters.get(adapter_key)
        if adapter is None:
            raise ValueError("Provider Adapter is not registered")
        result = ProviderInstanceService(PostgresProviderInstanceRepository()).create_draft(
            instance_key=str(payload.get("instanceKey") or ""), adapter_key=adapter_key,
            display_name=str(payload.get("displayName") or ""), config_schema_version=adapter.version,
            non_secret_config=dict(payload.get("config") or {}), actor_user_id=int(g.user_id),
        )
        return _ok(asdict(result), 201)
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/instances/<int:instance_id>", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def instance_detail(instance_id):
    row = PostgresDataSourceManagementRepository().get_instance(instance_id)
    return _ok(row) if row else _error(ValueError("Provider Instance not found"), 404)


@data_source_operations_blp.route("/instances/<int:instance_id>/credentials", methods=["POST"])
@login_required
@permission_required("data_sources:credentials")
def submit_credentials(instance_id):
    try:
        _require_step_up()
        payload = _payload()
        pepper = str(os.getenv("DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER") or "").strip()
        if not pepper:
            raise ValueError("Provider credential comparison pepper is not configured")
        service = ProviderCredentialService(
            load_default_data_routing_registry(), PostgresProviderCredentialRepository(),
            keyring=load_provider_credential_keyring(), comparison_pepper=pepper,
        )
        status = service.submit_and_validate(
            instance_id, dict(payload.get("credentials") or {}), actor_user_id=int(g.user_id),
            reason=_reason(payload),
        )
        return _ok(asdict(status))
    except StepUpRequiredError as exc:
        return _error(exc, 401)
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/instances/<int:instance_id>/<string:action>", methods=["POST"])
@login_required
@permission_required("data_sources:instances")
def instance_action(instance_id, action):
    try:
        payload = _payload()
        reason = _reason(payload)
        service = ProviderInstanceService(PostgresProviderInstanceRepository())
        if action == "activate":
            result = service.activate(instance_id, actor_user_id=int(g.user_id))
        elif action == "disable":
            result = service.disable(instance_id, actor_user_id=int(g.user_id), reason=reason)
        elif action == "retire":
            _require_step_up()
            result = service.retire(instance_id, actor_user_id=int(g.user_id), reason=reason)
        else:
            raise ValueError("Unsupported Provider Instance action")
        return _ok(asdict(result))
    except StepUpRequiredError as exc:
        return _error(exc, 401)
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/instances/<int:instance_id>/diagnostics/<string:capability_key>", methods=["POST"])
@login_required
@permission_required("data_sources:diagnostics")
def run_diagnostic(instance_id, capability_key):
    try:
        payload = _payload()
        entry = _diagnostic_entry(instance_id, capability_key)
        result = ProviderCapabilityTestService(
            load_default_data_routing_registry(), PostgresRouterSecretResolver(),
            PostgresDiagnosticRepository(), quota=QuotaManager(PostgresQuotaRepository()),
        ).run(
            entry, capability_key=capability_key, subject=dict(payload.get("subject") or {}),
            constraints=dict(payload.get("constraints") or {}), requested_by=int(g.user_id),
        )
        return _ok(asdict(result))
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/instances/<int:instance_id>/diagnostics", methods=["GET"])
@login_required
@permission_required("data_sources:diagnostics")
def latest_diagnostics(instance_id):
    try:
        return _ok({"items": PostgresDiagnosticRepository().latest_for_instance(instance_id)})
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/health/<int:state_id>/quarantine", methods=["POST"])
@login_required
@permission_required("data_sources:diagnostics")
def quarantine(state_id):
    try:
        payload = _payload()
        until = payload.get("until")
        parsed = datetime.fromisoformat(str(until).replace("Z", "+00:00")) if until else None
        PostgresDataSourceManagementRepository().quarantine(
            state_id=state_id, expected_version=int(payload.get("stateVersion") or 0),
            until=parsed, actor_user_id=int(g.user_id), reason=_reason(payload),
        )
        return _ok({"stateId": state_id})
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/health/<int:state_id>/extend-circuit", methods=["POST"])
@login_required
@permission_required("data_sources:diagnostics")
def extend_circuit(state_id):
    try:
        payload = _payload()
        until = datetime.fromisoformat(str(payload.get("until") or "").replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        PostgresDataSourceManagementRepository().extend_circuit(
            state_id=state_id, expected_version=int(payload.get("stateVersion") or 0),
            until=until, actor_user_id=int(g.user_id), reason=_reason(payload),
        )
        return _ok({"stateId": state_id})
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/health/<int:state_id>/recovery-probes", methods=["POST"])
@login_required
@permission_required("data_sources:diagnostics")
def recovery_probe(state_id):
    try:
        payload = _payload()
        reason = _reason(payload)
        repository = PostgresHealthRepository()
        with repository.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT instance_id,capability_key FROM qd_provider_health_states WHERE id=%s", (state_id,))
                row = cur.fetchone()
            finally:
                cur.close()
        if not row or not row.get("capability_key"):
            raise ValueError("Capability-scoped health state is required")
        capability = load_default_data_routing_registry().capabilities[str(row["capability_key"])]
        probe = ProviderHealthManager(repository).start_recovery_probe(
            capability, instance_id=int(row["instance_id"]), capability_key=str(row["capability_key"]),
            trigger="administrator", requested_by=int(g.user_id), reason=reason,
        )
        return _ok({**asdict(probe), "reason": reason}, 202)
    except (DataRoutingError, ValueError, KeyError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/policies", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def policies():
    return _ok({"items": PostgresDataSourceManagementRepository().list_policies()})


@data_source_operations_blp.route("/policies/<string:capability_key>", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def policy_detail(capability_key):
    row = PostgresDataSourceManagementRepository().get_policy(capability_key)
    return _ok(row) if row else _error(ValueError("Routing Policy not found"), 404)


def _policy_service():
    return RoutingPolicyService(load_default_data_routing_registry(), PostgresRoutingPolicyRepository())


@data_source_operations_blp.route("/policies/<string:capability_key>/draft", methods=["PUT"])
@login_required
@permission_required("data_sources:routing")
def save_policy_draft(capability_key):
    try:
        payload = _payload()
        state = _policy_service().save_draft(
            capability_key, payload.get("entries") or (), quality_profile=payload.get("qualityProfile") or {},
            expected_policy_version=payload.get("policyVersion"), actor_user_id=int(g.user_id),
        )
        return _ok(asdict(state))
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/policies/<string:capability_key>/preview", methods=["GET"])
@login_required
@permission_required("data_sources:routing")
def preview_policy(capability_key):
    try:
        return _ok(asdict(_policy_service().preview_draft(capability_key)))
    except DataRoutingError as exc:
        return _error(exc)


@data_source_operations_blp.route("/policies/<string:capability_key>/<string:action>", methods=["POST"])
@login_required
@permission_required("data_sources:routing")
def policy_action(capability_key, action):
    try:
        payload = _payload()
        reason = _reason(payload)
        version = int(payload.get("policyVersion") or 0)
        service = _policy_service()
        if action == "publish":
            state = service.publish_draft(capability_key, expected_policy_version=version, reason=reason, actor_user_id=int(g.user_id))
        elif action == "disable":
            state = service.disable_capability(capability_key, expected_policy_version=version, reason=reason, actor_user_id=int(g.user_id))
        elif action == "restore":
            state = service.restore_revision(
                capability_key, int(payload.get("revisionId") or 0),
                expected_policy_version=version, actor_user_id=int(g.user_id), reason=reason,
            )
        else:
            raise ValueError("Unsupported routing policy action")
        return _ok(asdict(state))
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/routed-requests/<string:routed_request_id>", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def routed_request_detail(routed_request_id):
    row = PostgresDataSourceManagementRepository().get_routed_request(routed_request_id)
    return _ok(row) if row else _error(ValueError("Routed Data Request not found"), 404)


@data_source_operations_blp.route("/legacy-imports", methods=["GET"])
@login_required
@permission_required("data_sources:view")
def legacy_import_status():
    repository = PostgresLegacyImportRepository()
    return _ok({"items": [asdict(item) for item in LegacyCredentialDetector(os.environ, repository).detect()]})


@data_source_operations_blp.route("/legacy-imports/<string:adapter_key>", methods=["POST"])
@login_required
@permission_required("data_sources:credentials")
def import_legacy_credential(adapter_key):
    try:
        _require_step_up(); payload = _payload(); reason = _reason(payload)
        pepper = str(os.getenv("DATA_PROVIDER_CREDENTIAL_COMPARISON_PEPPER") or "").strip()
        if not pepper:
            raise ValueError("Provider credential comparison pepper is not configured")
        registry = load_default_data_routing_registry()
        if adapter_key not in registry.adapters:
            raise ValueError("Provider Adapter is not registered")
        repository = PostgresLegacyImportRepository()
        result = LegacyCredentialImportService(
            LegacyCredentialDetector(os.environ, repository), repository,
            ProviderInstanceService(PostgresProviderInstanceRepository()),
            ProviderCredentialService(
                registry, PostgresProviderCredentialRepository(),
                keyring=load_provider_credential_keyring(), comparison_pepper=pepper,
            ),
        ).import_adapter(adapter_key, actor_user_id=int(g.user_id), reason=reason)
        return _ok(asdict(result), 201)
    except StepUpRequiredError as exc:
        return _error(exc, 401)
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


def _cutover_service():
    repository = PostgresCutoverRepository()
    return DataRoutingCutoverService(repository), repository


@data_source_operations_blp.route("/cutovers", methods=["POST"])
@login_required
@permission_required("data_sources:cutover")
def create_cutover():
    try:
        _require_step_up(); payload = _payload(); reason = _reason(payload)
        service, _ = _cutover_service()
        state = service.begin_preflight(str(payload.get("targetVersion") or ""), actor_user_id=int(g.user_id), reason=reason)
        return _ok(asdict(state), 201)
    except StepUpRequiredError as exc:
        return _error(exc, 401)
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)


@data_source_operations_blp.route("/cutovers/<int:cutover_id>/<string:action>", methods=["POST"])
@login_required
@permission_required("data_sources:cutover")
def cutover_action(cutover_id, action):
    try:
        _require_step_up(); payload = _payload(); reason = _reason(payload)
        service, repository = _cutover_service(); state = repository.get(cutover_id)
        if action == "evaluate":
            evidence = CutoverEvidenceCollector(PostgresPreflightEvidenceRepository()).collect()
            state = service.evaluate(state, evidence, actor_user_id=int(g.user_id))
        elif action == "maintenance":
            state = service.enter_maintenance(state, actor_user_id=int(g.user_id))
        elif action == "activate":
            state = service.activate(state, actor_user_id=int(g.user_id), **repository.drain_status())
        elif action == "abort":
            state = service.abort(state, actor_user_id=int(g.user_id), reason=reason)
        else:
            raise ValueError("Unsupported cutover action")
        return _ok(asdict(state))
    except StepUpRequiredError as exc:
        return _error(exc, 401)
    except (DataRoutingError, ValueError) as exc:
        return _error(exc)
