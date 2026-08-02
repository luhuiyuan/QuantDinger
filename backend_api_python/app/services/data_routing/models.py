"""Typed contracts shared by the data-routing control and execution planes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence


JsonObject = Mapping[str, Any]
NormalizedData = Any
QualityGate = Callable[[NormalizedData], Sequence[str]]
ConfigMigration = Callable[[JsonObject], JsonObject]


@dataclass(frozen=True, slots=True)
class AdapterFetchResult:
    payload: Any
    acquired_at: datetime
    warnings: tuple[Mapping[str, Any], ...] = ()
    quota_observations: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterErrorClassification:
    category: str
    retryable: bool
    permanence: str = "transient"
    scope_hint: str = "capability"
    retry_delay_seconds: float = 0.0


class ProviderAdapterRuntime(Protocol):
    """Provider-specific behavior required from every registered Adapter."""

    def resolve_account_identity(self, credentials: JsonObject, config: JsonObject) -> str | None: ...

    def diagnose(
        self,
        capability_key: str,
        subject: JsonObject,
        config: JsonObject,
        credentials: JsonObject,
    ) -> Any: ...

    def normalize(self, capability_key: str, payload: Any) -> NormalizedData: ...

    def supports_constraints(
        self,
        capability_key: str,
        constraints: JsonObject,
        config: JsonObject,
    ) -> bool: ...

    def fetch(
        self,
        capability_key: str,
        subject: JsonObject,
        constraints: JsonObject,
        config: JsonObject,
        credentials: JsonObject,
        deadline: datetime,
    ) -> AdapterFetchResult: ...

    def classify_error(self, capability_key: str, error: Exception) -> AdapterErrorClassification: ...

    def estimate_quota_cost(self, capability_key: str, operation: str, units: int) -> Mapping[str, float]: ...


@dataclass(frozen=True)
class ConfigMigrationStep:
    from_version: str
    to_version: str
    migrate: ConfigMigration


@dataclass(frozen=True)
class AdapterDefinition:
    key: str
    version: str
    public_name: str
    source_module: str
    config_schema: JsonObject
    credential_schema: JsonObject
    capabilities: frozenset[str]
    quota_contract: JsonObject
    runtime: ProviderAdapterRuntime
    config_migrations: Mapping[str, ConfigMigrationStep] = field(default_factory=dict)
    transport_retry_limit: int = 0


@dataclass(frozen=True)
class CapabilityDefinition:
    key: str
    version: str
    public_name: str
    semantic_family: str
    market: str
    source_module: str
    allowed_constraints: frozenset[str]
    normalized_contract: JsonObject
    hard_quality_gates: tuple[QualityGate, ...]
    strict_profiles: Mapping[str, JsonObject]
    cache_key_fields: tuple[str, ...]
    freshness_contract: JsonObject
    health_contract: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class MaterializedRegistration:
    key: str
    version: str
    code_fingerprint: str
    registration_status: str


@dataclass(frozen=True)
class InstanceMigrationCandidate:
    instance_id: int
    adapter_key: str
    config_schema_version: str
    config_version: int
    non_secret_config: JsonObject


@dataclass(frozen=True)
class ActiveRegistryReference:
    reference_type: str
    reference_id: str
    adapter_key: str | None = None
    capability_key: str | None = None


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    reference_type: str
    reference_id: str
    registry_key: str
    message: str


@dataclass(frozen=True)
class RegistrySyncReport:
    adapters_created_or_updated: tuple[str, ...] = ()
    capabilities_created_or_updated: tuple[str, ...] = ()
    adapters_tombstoned: tuple[str, ...] = ()
    capabilities_tombstoned: tuple[str, ...] = ()
    instances_migrated: tuple[int, ...] = ()
    instances_migration_required: tuple[int, ...] = ()
    readiness_issues: tuple[ReadinessIssue, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.readiness_issues
