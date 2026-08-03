from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.services.data_routing.credentials import (
    CapabilityVerification,
    CredentialInstanceContext,
    CredentialStatus,
    CrossAccountRotationError,
    DuplicateProviderCredentialError,
    ProviderAccountConflictError,
    ProviderCapabilityValidationError,
    ProviderCredentialError,
    ProviderCredentialService,
    StoredCredentialSecret,
)
from app.services.data_routing.models import (
    AdapterDefinition,
    AdapterErrorClassification,
    AdapterFetchResult,
    CapabilityDefinition,
)
from app.services.data_routing.registry import DataRoutingRegistry
from app.utils.credential_crypto import ProviderCredentialKeyring


class Runtime:
    def __init__(self):
        self.identity = "account-a"
        self.results = {"quote": {"ok": True}, "history": {"ok": True}}
        self.error = None

    def resolve_account_identity(self, credentials, config):
        if self.error:
            raise self.error
        return self.identity

    def diagnose(self, capability_key, subject, config, credentials):
        value = self.results[capability_key]
        if isinstance(value, Exception):
            raise value
        return value

    def normalize(self, capability_key, payload): return payload
    def supports_constraints(self, capability_key, constraints, config): return True
    def fetch(self, capability_key, subject, constraints, config, credentials, deadline):
        return AdapterFetchResult({"ok": True}, deadline)
    def classify_error(self, capability_key, error):
        return AdapterErrorClassification("provider_error", False)
    def estimate_quota_cost(self, capability_key, operation, units): return {"requests": units}


def gate(value): return []


def build_registry(runtime, *, credential_schema=None):
    registry = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    for key in ("quote", "history"):
        registry.register_capability(CapabilityDefinition(
            key, "1", key.title(), key, "US", "tests.adapters.example",
            frozenset({"symbol"}), {"type": key}, (gate,), {}, ("subject", "symbol"), {"fresh_seconds": 60},
        ))
    registry.register_adapter(AdapterDefinition(
        "example", "1", "Example", "tests.adapters.example",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        credential_schema or {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"], "additionalProperties": False},
        frozenset({"quote", "history"}), {"buckets": ["requests"]}, runtime,
    ))
    registry.freeze()
    return registry


class Repo:
    def __init__(self):
        self.instances = {1: CredentialInstanceContext(1, "example", "draft", None, {})}
        self.rows = []
        self.tags = {}
        self.capabilities = {}
        self.audits = []
        self.next_id = 1
        self.other_identity = None
        self.other_unidentified = None
        self.due = []
        self.activate_error = None

    def get_instance_context(self, instance_id): return self.instances.get(instance_id)
    def find_duplicate_secret_tag(self, secret_tag): return self.tags.get(secret_tag)
    def stage_pending_credential(self, **values):
        for row in self.rows:
            if row.status == "pending" and row.instance_id == values["instance_id"]:
                row.status = "destroyed"; row.ciphertext = ""; self.tags.pop(row.secret_tag, None); row.secret_tag=f"discarded:{row.credential_id}"
        row = Row(self.next_id, values["instance_id"], 1 + max([r.credential_version for r in self.rows if r.instance_id == values["instance_id"]] or [0]),
                  values["schema_version"], "pending", values["key_id"], values["ciphertext"], values["secret_tag"])
        self.next_id += 1; self.rows.append(row); self.tags[values["secret_tag"]] = values["instance_id"]
        self.audits.append({"action": "submitted", "instance_id": values["instance_id"], "key_id": values["key_id"]})
        return row.secret()
    def destroy_pending_credential(self, credential_id, **kwargs):
        row=next(r for r in self.rows if r.credential_id==credential_id); self.tags.pop(row.secret_tag,None)
        row.status="destroyed"; row.ciphertext=""; row.secret_tag=f"discarded:{row.credential_id}"
        self.audits.append({"action": "destroyed", "instance_id": row.instance_id})
    def get_active_credential(self, instance_id):
        row=next((r for r in self.rows if r.instance_id==instance_id and r.status=="active"),None); return row.secret() if row else None
    def record_pending_validation(self, credential_id, results, *, initial_credential):
        row=next(r for r in self.rows if r.credential_id==credential_id); row.validation=tuple(results)
        if initial_credential: self.instances[row.instance_id]=replace(self.instances[row.instance_id], lifecycle_status="validation_failed")
    def find_account_identity_conflict(self, adapter_key, identity, *, exclude_instance_id): return self.other_identity
    def find_other_unidentified_active(self, adapter_key, *, exclude_instance_id): return self.other_unidentified
    def activate_validated_pending(self, **values):
        if self.activate_error:
            raise self.activate_error
        for row in self.rows:
            if row.instance_id==values["instance"].instance_id and row.status=="active": row.status="destroyed"; row.ciphertext=""
        row=next(r for r in self.rows if r.credential_id==values["pending"].credential_id); row.status="active"
        instance=values["instance"]
        lifecycle_status = "active" if instance.lifecycle_status in {"draft", "validation_failed"} else instance.lifecycle_status
        self.instances[instance.instance_id]=replace(instance,lifecycle_status=lifecycle_status,provider_account_identity=values["provider_account_identity"])
        for result in values["results"]:
            key = (instance.instance_id,result.capability_key)
            if self.capabilities.get(key) != "disabled":
                self.capabilities[key] = result.outcome
        self.audits.append({"action":"activated","instance_id":instance.instance_id,"capabilities":sorted(r.capability_key for r in values["results"] if r.eligible)})
        return self.get_credential_status(instance.instance_id)
    def get_credential_status(self, instance_id):
        active=next((r for r in self.rows if r.instance_id==instance_id and r.status=="active"),None)
        pending=next((r for r in self.rows if r.instance_id==instance_id and r.status=="pending"),None)
        return CredentialStatus(bool(active), active.credential_version if active else None, pending.credential_version if pending else None,
                                active.encryption_key_id if active else None, pending.encryption_key_id if pending else None)
    def mark_capabilities_for_revalidation(self, instance_id, capability_keys, *, trigger):
        for key in capability_keys:
            if trigger != "periodic" and self.capabilities.get((instance_id,key)) != "disabled": self.capabilities[(instance_id,key)]="unverified"
    def apply_capability_revalidation(self, instance_id, results, *, trigger):
        for result in results:
            old=self.capabilities.get((instance_id,result.capability_key))
            if old=="disabled": continue
            if result.outcome != "transient_failure": self.capabilities[(instance_id,result.capability_key)]=result.outcome
    def disable_capability(self, instance_id, capability_key, **kwargs):
        if (instance_id,capability_key) not in self.capabilities: raise ProviderCredentialError("not verified")
        self.capabilities[(instance_id,capability_key)]="disabled"
        self.audits.append({"action":"disabled","capability":capability_key})
    def list_due_revalidation_instances(self, *, limit): return self.due[:limit]


class Row:
    def __init__(self, credential_id, instance_id, version, schema, status, key_id, ciphertext, tag):
        self.credential_id=credential_id; self.instance_id=instance_id; self.credential_version=version
        self.credential_schema_version=schema; self.status=status; self.encryption_key_id=key_id
        self.ciphertext=ciphertext; self.secret_tag=tag; self.validation=()
    def secret(self):
        return StoredCredentialSecret(self.credential_id,self.credential_version,self.credential_schema_version,self.status,self.encryption_key_id,self.ciphertext)


@pytest.fixture
def setup():
    runtime=Runtime(); repo=Repo(); keyring=ProviderCredentialKeyring("active",{"active":"encryption-secret"})
    service=ProviderCredentialService(build_registry(runtime),repo,keyring=keyring,comparison_pepper="comparison-pepper")
    return service,repo,runtime


def test_initial_submission_is_write_only_and_activates_verified_subset(setup):
    service,repo,runtime=setup; runtime.results["history"]={"ok":False,"permanent":True,"code":"plan_denied"}
    status=service.submit_and_validate(1,{"token":"top-secret"},actor_user_id=7,reason="initial setup")
    assert status.configured and status.active_version==1 and status.pending_version is None
    assert not hasattr(status,"ciphertext") and not hasattr(status,"secret_comparison_tag")
    assert repo.instances[1].provider_account_identity=="account-a"
    assert repo.capabilities[(1,"quote")]=="eligible" and repo.capabilities[(1,"history")]=="ineligible"
    assert "top-secret" not in repr(status) and "top-secret" not in repr(repo.audits)


def test_exact_credential_reuse_is_rejected_even_for_same_instance(setup):
    service,repo,_=setup
    service.submit_and_validate(1,{"token":"same"},actor_user_id=7,reason="initial")
    with pytest.raises(DuplicateProviderCredentialError):
        service.submit_and_validate(1,{"token":"same"},actor_user_id=7,reason="rotation")


def test_credentialless_provider_does_not_treat_empty_bundle_as_reused_secret():
    runtime = Runtime()
    runtime.identity = None
    repo = Repo()
    empty_schema = {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }
    registry = build_registry(runtime, credential_schema=empty_schema)
    keyring = ProviderCredentialKeyring("active", {"active": "encryption-secret"})
    service = ProviderCredentialService(
        registry, repo, keyring=keyring, comparison_pepper="comparison-pepper",
    )
    empty_tag = __import__(
        "app.utils.credential_crypto", fromlist=["provider_credential_comparison_tag"]
    ).provider_credential_comparison_tag("{}", pepper="comparison-pepper")
    repo.tags[empty_tag] = 99

    status = service.submit_and_validate(1, {}, actor_user_id=7, reason="public provider setup")

    assert status.configured is True


def test_failed_rotation_keeps_old_active_and_pending_for_correction(setup):
    service,repo,runtime=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    runtime.results={"quote":RuntimeError("leaked-new-secret"),"history":RuntimeError("leaked-new-secret")}
    with pytest.raises(ProviderCapabilityValidationError):
        service.submit_and_validate(1,{"token":"new-secret"},actor_user_id=7,reason="rotate")
    active=next(r for r in repo.rows if r.status=="active"); pending=next(r for r in repo.rows if r.status=="pending")
    assert active.ciphertext and pending.ciphertext
    assert "leaked-new-secret" not in repr(pending.validation) and "new-secret" not in repr(repo.audits)


def test_successful_rotation_destroys_old_ciphertext_and_rejects_cross_account(setup):
    service,repo,runtime=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    old=next(r for r in repo.rows if r.status=="active")
    runtime.identity="account-b"
    with pytest.raises(CrossAccountRotationError,match="new Provider Instance"):
        service.submit_and_validate(1,{"token":"other-account"},actor_user_id=7,reason="rotate")
    assert old.status=="active" and old.ciphertext
    assert next(r for r in repo.rows if r.credential_id==2).ciphertext==""
    repo.instances[2]=CredentialInstanceContext(2,"example","draft",None,{})
    service.submit_and_validate(2,{"token":"other-account"},actor_user_id=7,reason="new instance")
    runtime.identity="account-a"
    service.submit_and_validate(1,{"token":"replacement"},actor_user_id=7,reason="rotate")
    assert old.status=="destroyed" and old.ciphertext==""


@pytest.mark.parametrize("lifecycle_status", ["disabled", "draining", "migration_required"])
def test_rotation_preserves_non_activation_instance_lifecycle(setup, lifecycle_status):
    service,repo,_=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    repo.instances[1]=replace(repo.instances[1], lifecycle_status=lifecycle_status)

    service.submit_and_validate(1,{"token":"replacement"},actor_user_id=7,reason="rotate")

    assert repo.instances[1].lifecycle_status == lifecycle_status


def test_rotation_preserves_admin_disabled_capability(setup):
    service,repo,_=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    service.disable_capability(1,"quote",actor_user_id=7,reason="operator disabled")

    service.submit_and_validate(1,{"token":"replacement"},actor_user_id=7,reason="rotate")

    assert repo.capabilities[(1,"quote")] == "disabled"


def test_atomic_activation_failure_preserves_old_active_ciphertext(setup):
    service,repo,_=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    old=next(r for r in repo.rows if r.status=="active")
    repo.activate_error=RuntimeError("audit write failed")
    with pytest.raises(RuntimeError,match="audit write failed"):
        service.submit_and_validate(1,{"token":"replacement"},actor_user_id=7,reason="rotate")
    assert old.status=="active" and old.ciphertext
    assert any(row.status=="pending" and row.ciphertext for row in repo.rows)


def test_duplicate_account_and_unidentified_single_instance_are_rejected(setup):
    service,repo,runtime=setup; repo.other_identity=2
    with pytest.raises(ProviderAccountConflictError): service.submit_and_validate(1,{"token":"one"},actor_user_id=7,reason="initial")
    runtime.identity=None; repo.other_identity=None; repo.other_unidentified=2
    with pytest.raises(ProviderAccountConflictError): service.submit_and_validate(1,{"token":"two"},actor_user_id=7,reason="initial")


def test_revalidation_preserves_eligibility_on_transient_failure_and_admin_cannot_grant(setup):
    service,repo,runtime=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    runtime.results["quote"]=RuntimeError("temporary")
    service.run_revalidation(1,trigger="periodic",capability_keys=["quote"])
    assert repo.capabilities[(1,"quote")]=="eligible"
    service.disable_capability(1,"quote",actor_user_id=7,reason="operator quarantine")
    assert repo.capabilities[(1,"quote")]=="disabled"
    with pytest.raises(ProviderCredentialError): service.disable_capability(1,"missing",actor_user_id=7,reason="no grant")


@pytest.mark.parametrize("trigger", ["configuration_change", "credential_change", "permission_evidence"])
def test_configuration_credential_and_permission_triggers_revoke_stale_eligibility(setup, trigger):
    service,repo,_=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    service.request_revalidation(1,trigger=trigger,capability_keys=["quote"])
    assert repo.capabilities[(1,"quote")]=="unverified"


def test_periodic_revalidation_is_bounded(setup):
    service,repo,_=setup
    service.submit_and_validate(1,{"token":"old"},actor_user_id=7,reason="initial")
    repo.due=[1]
    assert list(service.revalidate_due(limit=100))==[1]
    with pytest.raises(ProviderCredentialError,match="invalid"):
        service.request_revalidation(1,trigger="administrator_grant")


def test_credential_modules_do_not_log_or_serialize_secret_fields():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/"app"/"services"/"data_routing"
    service_source=(root/"credentials.py").read_text()
    repository_source=(root/"credential_repository.py").read_text()
    assert "logger." not in service_source and "logger." not in repository_source
    assert '"ciphertext":' not in repository_source
    assert '"secret_comparison_tag":' not in repository_source


def test_activation_sql_preserves_instance_and_admin_disabled_capability_states():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "data_routing"
        / "credential_repository.py"
    ).read_text()
    assert "WHEN lifecycle_status IN ('draft','validation_failed')" in source
    assert "ELSE lifecycle_status END" in source
    assert "qd_provider_instance_capabilities.eligibility_status='disabled'" in source
    assert "THEN qd_provider_instance_capabilities.disabled_reason" in source
    assert "pg_advisory_xact_lock_shared" in source


def test_raw_database_error_is_sanitized_before_leaving_connection_context():
    from app.services.data_routing.credential_repository import PostgresProviderCredentialRepository

    secret = "gAAAAAB-sensitive-ciphertext"

    class Cursor:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError(f"database rejected ciphertext {secret}")

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

    class Factory:
        def __init__(self):
            self.seen_exception = None

        def __call__(self):
            factory = self

            class Manager:
                def __enter__(self):
                    return Connection()

                def __exit__(self, exc_type, exc, traceback):
                    factory.seen_exception = exc
                    return False

            return Manager()

    factory = Factory()
    repository = PostgresProviderCredentialRepository(factory)

    with pytest.raises(ProviderCredentialError) as caught:
        repository.stage_pending_credential(
            instance_id=1,
            schema_version="1",
            key_id="active",
            ciphertext=secret,
            secret_tag="comparison-tag",
            actor_user_id=1,
            reason="rotate",
        )

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert isinstance(factory.seen_exception, ProviderCredentialError)
    assert secret not in str(factory.seen_exception)


def test_duplicate_detection_uses_the_named_database_constraint_only():
    from app.services.data_routing.credential_repository import _is_secret_reuse_violation

    class Error(Exception):
        pass

    duplicate = Error("ciphertext and unique should never be parsed")
    duplicate.diag = type("Diag", (), {"constraint_name": "idx_provider_credentials_secret_reuse"})()
    unrelated = Error("unique secret ciphertext")
    unrelated.diag = type("Diag", (), {"constraint_name": "another_constraint"})()

    assert _is_secret_reuse_violation(duplicate) is True
    assert _is_secret_reuse_violation(unrelated) is False
