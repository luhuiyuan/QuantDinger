from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.data_routing.errors import RegistryConflictError, RegistryValidationError
from app.services.data_routing.models import (
    ActiveRegistryReference,
    AdapterErrorClassification,
    AdapterFetchResult,
    AdapterDefinition,
    CapabilityDefinition,
    ConfigMigrationStep,
    InstanceMigrationCandidate,
    MaterializedRegistration,
)
from app.services.data_routing.registry import DataRoutingRegistry
from app.services.data_routing.repository import RegistryMaterializer


OBJECT_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


class ExampleRuntime:
    def resolve_account_identity(self, credentials, config):
        return "account-1"

    def diagnose(self, capability_key, subject, config, credentials):
        return {"ok": True}

    def normalize(self, capability_key, payload):
        return payload

    def supports_constraints(self, capability_key, constraints, config):
        return True

    def fetch(self, capability_key, subject, constraints, config, credentials, deadline):
        return AdapterFetchResult({"ok": True}, deadline)

    def classify_error(self, capability_key, error):
        return AdapterErrorClassification("provider_error", False)

    def estimate_quota_cost(self, capability_key, operation, units):
        return {"requests": float(units)}


def hard_gate(value):
    return [] if value is not None else ["missing"]


def capability(version="1"):
    return CapabilityDefinition(
        key="example_quote",
        version=version,
        public_name="Example quote",
        semantic_family="quote",
        market="US",
        source_module="tests.adapters.example",
        allowed_constraints=frozenset({"symbol", "max_delay"}),
        normalized_contract={"type": "quote"},
        hard_quality_gates=(hard_gate,),
        strict_profiles={"strict": {"max_delay": 5}},
        cache_key_fields=("subject", "symbol"),
        freshness_contract={"fresh_seconds": 5},
    )


def adapter(version="1", migrations=None):
    return AdapterDefinition(
        key="example",
        version=version,
        public_name="Example",
        source_module="tests.adapters.example",
        config_schema=OBJECT_SCHEMA,
        credential_schema=OBJECT_SCHEMA,
        capabilities=frozenset({"example_quote"}),
        quota_contract={"buckets": ["requests"]},
        runtime=ExampleRuntime(),
        config_migrations=migrations or {},
    )


def registry(adapter_definition=None, capability_definition=None):
    value = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    value.register_capability(capability_definition or capability())
    value.register_adapter(adapter_definition or adapter())
    value.freeze()
    return value


class FakeRepository:
    def __init__(self):
        self.adapters = {}
        self.capabilities = {}
        self.candidates = []
        self.references = []
        self.tombstoned_adapters = []
        self.tombstoned_capabilities = []
        self.updated_instances = []
        self.migration_required = []

    def list_materialized_adapters(self):
        return self.adapters

    def list_materialized_capabilities(self):
        return self.capabilities

    def upsert_adapter(self, definition, fingerprint):
        self.adapters[definition.key] = MaterializedRegistration(definition.key, definition.version, fingerprint, "registered")

    def upsert_capability(self, definition, fingerprint):
        self.capabilities[definition.key] = MaterializedRegistration(definition.key, definition.version, fingerprint, "registered")

    def tombstone_adapter(self, key):
        self.tombstoned_adapters.append(key)

    def tombstone_capability(self, key):
        self.tombstoned_capabilities.append(key)

    def list_instance_migration_candidates(self):
        return self.candidates

    def update_instance_config(self, candidate, target_version, config):
        self.updated_instances.append((candidate.instance_id, target_version, dict(config)))
        return True

    def mark_instance_migration_required(self, instance_id):
        self.migration_required.append(instance_id)

    def list_active_registry_references(self):
        return self.references


def test_registry_rejects_dynamic_sources_invalid_schema_and_duplicates():
    value = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    with pytest.raises(RegistryValidationError, match="outside trusted"):
        value.register_capability(replace(capability(), source_module="database.dynamic"))
    with pytest.raises(RegistryValidationError, match="reject undeclared"):
        value.register_adapter(replace(adapter(), config_schema={"type": "object", "additionalProperties": True}))

    value.register_capability(capability())
    with pytest.raises(RegistryConflictError, match="duplicate"):
        value.register_capability(capability())


def test_registry_requires_runtime_quota_normalization_diagnostic_and_identity_contracts():
    broken = replace(adapter(), runtime=object())
    value = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    with pytest.raises(RegistryValidationError, match="resolve_account_identity"):
        value.register_adapter(broken)


def test_registry_freeze_rejects_unknown_capability_and_late_registration():
    value = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    value.register_adapter(adapter())
    with pytest.raises(RegistryValidationError, match="unknown Capability"):
        value.freeze()

    value.register_capability(capability())
    value.freeze()
    with pytest.raises(RegistryConflictError, match="frozen"):
        value.register_capability(replace(capability(), key="another_quote"))


def test_materialization_is_idempotent_detects_versions_and_tombstones_removed_metadata():
    repo = FakeRepository()
    first = RegistryMaterializer(registry(), repo).sync()
    second = RegistryMaterializer(registry(), repo).sync()

    assert first.adapters_created_or_updated == ("example",)
    assert first.capabilities_created_or_updated == ("example_quote",)
    assert second.adapters_created_or_updated == ()
    assert second.capabilities_created_or_updated == ()

    repo.adapters["removed"] = MaterializedRegistration("removed", "1", "old", "registered")
    repo.capabilities["removed_data"] = MaterializedRegistration("removed_data", "1", "old", "registered")
    report = RegistryMaterializer(registry(adapter(version="2")), repo).sync()
    assert report.adapters_created_or_updated == ("example",)
    assert report.adapters_tombstoned == ("removed",)
    assert report.capabilities_tombstoned == ("removed_data",)


def test_deterministic_config_upgrade_updates_unreferenced_instance():
    step = ConfigMigrationStep("1", "2", lambda config: {**config, "timeout": int(config.get("timeout", 5))})
    repo = FakeRepository()
    repo.candidates = [InstanceMigrationCandidate(7, "example", "1", 3, {"timeout": "8"})]

    report = RegistryMaterializer(registry(adapter(version="2", migrations={"1": step})), repo).sync()

    assert report.instances_migrated == (7,)
    assert report.instances_migration_required == ()
    assert repo.updated_instances == [(7, "2", {"timeout": 8})]


def test_missing_or_nondeterministic_migration_marks_instance_and_blocks_readiness():
    calls = {"count": 0}

    def unstable(config):
        calls["count"] += 1
        return {**config, "nonce": calls["count"]}

    step = ConfigMigrationStep("1", "2", unstable)
    repo = FakeRepository()
    repo.candidates = [InstanceMigrationCandidate(9, "example", "1", 1, {})]
    report = RegistryMaterializer(registry(adapter(version="2", migrations={"1": step})), repo).sync()

    assert report.instances_migration_required == (9,)
    assert repo.migration_required == [9]
    assert report.ready is False
    assert report.readiness_issues[0].code == "migration_required"


def test_active_references_to_removed_registry_keys_block_readiness():
    repo = FakeRepository()
    repo.references = [
        ActiveRegistryReference("provider_instance", "11", adapter_key="removed"),
        ActiveRegistryReference("routing_policy", "12", capability_key="removed_data"),
    ]

    report = RegistryMaterializer(registry(), repo).sync()

    assert report.ready is False
    assert {issue.code for issue in report.readiness_issues} == {"missing_adapter", "missing_capability"}


def test_default_registry_bootstrap_is_explicit_and_idempotent():
    from app.services.data_routing import bootstrap

    first = bootstrap.load_default_data_routing_registry()
    second = bootstrap.load_default_data_routing_registry()

    assert first is second


def test_migration_command_applies_schema_before_registry_materialization(monkeypatch):
    from app.commands import migrate
    from app.utils import db

    calls = []
    report = type("Report", (), {
        "adapters_created_or_updated": (),
        "capabilities_created_or_updated": (),
        "adapters_tombstoned": (),
        "capabilities_tombstoned": (),
        "instances_migrated": (),
        "readiness_issues": (),
        "ready": True,
    })()
    monkeypatch.setattr(db, "init_database", lambda *, strict_migrations: calls.append(("schema", strict_migrations)))
    monkeypatch.setattr(migrate, "_sync_data_routing_registry", lambda: calls.append(("registry", True)) or report)

    migrate.main()

    assert calls == [("schema", True), ("registry", True)]
