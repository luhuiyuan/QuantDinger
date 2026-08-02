"""Registry materialization repository and startup consistency service."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Protocol, Sequence

from .errors import ConfigMigrationError
from .models import (
    ActiveRegistryReference,
    InstanceMigrationCandidate,
    MaterializedRegistration,
    ReadinessIssue,
    RegistrySyncReport,
)
from .registry import DataRoutingRegistry


class RegistryRepository(Protocol):
    def list_materialized_adapters(self) -> Mapping[str, MaterializedRegistration]: ...
    def list_materialized_capabilities(self) -> Mapping[str, MaterializedRegistration]: ...
    def upsert_adapter(self, definition, fingerprint: str) -> None: ...
    def upsert_capability(self, definition, fingerprint: str) -> None: ...
    def tombstone_adapter(self, key: str) -> None: ...
    def tombstone_capability(self, key: str) -> None: ...
    def list_instance_migration_candidates(self) -> Sequence[InstanceMigrationCandidate]: ...
    def update_instance_config(self, candidate: InstanceMigrationCandidate, target_version: str, config: Mapping[str, Any]) -> bool: ...
    def mark_instance_migration_required(self, instance_id: int) -> None: ...
    def list_active_registry_references(self) -> Sequence[ActiveRegistryReference]: ...


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class RegistryMaterializer:
    def __init__(self, registry: DataRoutingRegistry, repository: RegistryRepository):
        self.registry = registry
        self.repository = repository

    def _migrate_candidate(self, candidate: InstanceMigrationCandidate) -> bool:
        adapter = self.registry.adapters.get(candidate.adapter_key)
        if adapter is None:
            return False
        version = candidate.config_schema_version
        config = dict(candidate.non_secret_config)
        visited: set[str] = set()
        while version != adapter.version:
            if version in visited:
                raise ConfigMigrationError("Adapter config migration cycle", details={"adapter": adapter.key})
            visited.add(version)
            step = adapter.config_migrations.get(version)
            if step is None:
                return False
            first = dict(step.migrate(dict(config)))
            second = dict(step.migrate(dict(config)))
            if _canonical(first) != _canonical(second):
                raise ConfigMigrationError("Adapter config migration is not deterministic", details={"adapter": adapter.key})
            config = first
            version = step.to_version
        return self.repository.update_instance_config(candidate, adapter.version, config)

    def sync(self) -> RegistrySyncReport:
        adapters_before = self.repository.list_materialized_adapters()
        capabilities_before = self.repository.list_materialized_capabilities()
        adapters_changed: list[str] = []
        capabilities_changed: list[str] = []
        adapters_tombstoned: list[str] = []
        capabilities_tombstoned: list[str] = []

        for key, definition in sorted(self.registry.adapters.items()):
            fingerprint = self.registry.adapter_fingerprint(key)
            previous = adapters_before.get(key)
            if previous is None or previous.version != definition.version or previous.code_fingerprint != fingerprint or previous.registration_status != "registered":
                self.repository.upsert_adapter(definition, fingerprint)
                adapters_changed.append(key)
        for key in sorted(set(adapters_before) - set(self.registry.adapters)):
            self.repository.tombstone_adapter(key)
            adapters_tombstoned.append(key)

        for key, definition in sorted(self.registry.capabilities.items()):
            fingerprint = self.registry.capability_fingerprint(key)
            previous = capabilities_before.get(key)
            if previous is None or previous.version != definition.version or previous.code_fingerprint != fingerprint or previous.registration_status != "registered":
                self.repository.upsert_capability(definition, fingerprint)
                capabilities_changed.append(key)
        for key in sorted(set(capabilities_before) - set(self.registry.capabilities)):
            self.repository.tombstone_capability(key)
            capabilities_tombstoned.append(key)

        migrated: list[int] = []
        migration_required: list[int] = []
        for candidate in self.repository.list_instance_migration_candidates():
            try:
                success = self._migrate_candidate(candidate)
            except ConfigMigrationError:
                success = False
            if success:
                migrated.append(candidate.instance_id)
            else:
                self.repository.mark_instance_migration_required(candidate.instance_id)
                migration_required.append(candidate.instance_id)

        issues: list[ReadinessIssue] = []
        for reference in self.repository.list_active_registry_references():
            if reference.adapter_key and reference.adapter_key not in self.registry.adapters:
                issues.append(ReadinessIssue("missing_adapter", reference.reference_type, reference.reference_id, reference.adapter_key, "Active reference uses an Adapter absent from code registry"))
            if reference.capability_key and reference.capability_key not in self.registry.capabilities:
                issues.append(ReadinessIssue("missing_capability", reference.reference_type, reference.reference_id, reference.capability_key, "Active reference uses a Capability absent from code registry"))
        for instance_id in migration_required:
            issues.append(ReadinessIssue("migration_required", "provider_instance", str(instance_id), str(instance_id), "Provider Instance configuration requires a supported deterministic migration"))

        return RegistrySyncReport(
            tuple(adapters_changed), tuple(capabilities_changed), tuple(adapters_tombstoned), tuple(capabilities_tombstoned),
            tuple(migrated), tuple(migration_required), tuple(issues),
        )


class PostgresRegistryRepository:
    """Small SQL adapter; transaction ownership stays with each repository call."""

    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection
            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(sql, params)
                return list(cur.fetchall() or [])
            finally:
                cur.close()

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(sql, params)
                count = cur.rowcount
                db.commit()
                return count
            finally:
                cur.close()

    def list_materialized_adapters(self):
        rows = self._fetchall("SELECT adapter_key AS key, adapter_version AS version, code_fingerprint, registration_status FROM qd_data_adapter_types")
        return {row["key"]: MaterializedRegistration(**row) for row in rows}

    def list_materialized_capabilities(self):
        rows = self._fetchall("SELECT capability_key AS key, capability_version AS version, code_fingerprint, registration_status FROM qd_data_capabilities")
        return {row["key"]: MaterializedRegistration(**row) for row in rows}

    def upsert_adapter(self, definition, fingerprint: str) -> None:
        self._execute("""INSERT INTO qd_data_adapter_types
            (adapter_key, adapter_version, public_name, config_schema, credential_schema, registration_status, code_fingerprint, tombstoned_at)
            VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,'registered',%s,NULL)
            ON CONFLICT (adapter_key) DO UPDATE SET adapter_version=EXCLUDED.adapter_version, public_name=EXCLUDED.public_name,
            config_schema=EXCLUDED.config_schema, credential_schema=EXCLUDED.credential_schema,
            registration_status='registered', code_fingerprint=EXCLUDED.code_fingerprint, tombstoned_at=NULL, updated_at=NOW()""",
            (definition.key, definition.version, definition.public_name, json.dumps(definition.config_schema), json.dumps(definition.credential_schema), fingerprint))

    def upsert_capability(self, definition, fingerprint: str) -> None:
        self._execute("""INSERT INTO qd_data_capabilities
            (capability_key, capability_version, public_name, semantic_family, market, normalized_contract,
             allowed_constraints, hard_quality_contract, cache_key_contract, registration_status, code_fingerprint, tombstoned_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,'registered',%s,NULL)
            ON CONFLICT (capability_key) DO UPDATE SET capability_version=EXCLUDED.capability_version,
            public_name=EXCLUDED.public_name, semantic_family=EXCLUDED.semantic_family, market=EXCLUDED.market,
            normalized_contract=EXCLUDED.normalized_contract, allowed_constraints=EXCLUDED.allowed_constraints,
            hard_quality_contract=EXCLUDED.hard_quality_contract, cache_key_contract=EXCLUDED.cache_key_contract,
            registration_status='registered', code_fingerprint=EXCLUDED.code_fingerprint, tombstoned_at=NULL, updated_at=NOW()""",
            (definition.key, definition.version, definition.public_name, definition.semantic_family, definition.market,
             json.dumps(definition.normalized_contract), json.dumps(sorted(definition.allowed_constraints)),
             json.dumps({"gate_count": len(definition.hard_quality_gates), "strict_profiles": definition.strict_profiles}),
             json.dumps({"fields": definition.cache_key_fields, "freshness": definition.freshness_contract}), fingerprint))

    def tombstone_adapter(self, key: str) -> None:
        self._execute("UPDATE qd_data_adapter_types SET registration_status='tombstoned', tombstoned_at=COALESCE(tombstoned_at,NOW()), updated_at=NOW() WHERE adapter_key=%s", (key,))

    def tombstone_capability(self, key: str) -> None:
        self._execute("UPDATE qd_data_capabilities SET registration_status='tombstoned', tombstoned_at=COALESCE(tombstoned_at,NOW()), updated_at=NOW() WHERE capability_key=%s", (key,))

    def list_instance_migration_candidates(self):
        rows = self._fetchall("""SELECT pi.id AS instance_id, pi.adapter_key, pi.config_schema_version,
            pi.config_version, pi.non_secret_config FROM qd_provider_instances pi
            JOIN qd_data_adapter_types at ON at.adapter_key=pi.adapter_key
            WHERE pi.lifecycle_status <> 'retired' AND pi.config_schema_version <> at.adapter_version""")
        return [InstanceMigrationCandidate(**row) for row in rows]

    def update_instance_config(self, candidate, target_version: str, config: Mapping[str, Any]) -> bool:
        return self._execute("""UPDATE qd_provider_instances SET non_secret_config=%s::jsonb,
            config_schema_version=%s, config_version=config_version+1, updated_at=NOW()
            WHERE id=%s AND config_version=%s""", (json.dumps(config), target_version, candidate.instance_id, candidate.config_version)) == 1

    def mark_instance_migration_required(self, instance_id: int) -> None:
        self._execute("UPDATE qd_provider_instances SET lifecycle_status='migration_required', updated_at=NOW() WHERE id=%s AND lifecycle_status <> 'retired'", (instance_id,))

    def list_active_registry_references(self):
        rows = self._fetchall("""SELECT 'provider_instance' AS reference_type, pi.id::text AS reference_id,
            pi.adapter_key, pic.capability_key FROM qd_provider_instances pi
            LEFT JOIN qd_provider_instance_capabilities pic ON pic.instance_id=pi.id
            WHERE pi.lifecycle_status IN ('active','draining','disabled','migration_required')
            UNION ALL
            SELECT 'routing_policy', p.id::text, NULL, p.capability_key FROM qd_data_routing_policies p
            WHERE p.effective_revision_id IS NOT NULL OR NOT p.enabled""")
        return [ActiveRegistryReference(**row) for row in rows]
