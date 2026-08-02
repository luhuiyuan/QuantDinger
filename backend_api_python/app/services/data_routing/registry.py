"""Trusted code registry for Provider Adapters and Data Capabilities."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import asdict
from typing import Any, Iterable, Mapping

from .errors import RegistryConflictError, RegistryValidationError
from .models import AdapterDefinition, CapabilityDefinition


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,119}$")
_REQUIRED_RUNTIME_METHODS = (
    "resolve_account_identity",
    "diagnose",
    "normalize",
    "supports_constraints",
    "fetch",
    "classify_error",
    "estimate_quota_cost",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _validate_key(kind: str, key: str) -> None:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise RegistryValidationError(f"invalid {kind} key", details={"key": key})


def _validate_version(kind: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise RegistryValidationError(f"invalid {kind} version", details={"version": value})


def _validate_object_schema(name: str, schema: Mapping[str, Any]) -> None:
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise RegistryValidationError(f"{name} must be a JSON object schema")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise RegistryValidationError(f"{name}.properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item in properties for item in required):
        raise RegistryValidationError(f"{name}.required must reference declared properties")
    if schema.get("additionalProperties", False) is not False:
        raise RegistryValidationError(f"{name} must reject undeclared properties")


class DataRoutingRegistry:
    """Mutable during process bootstrap, then frozen for deterministic use."""

    def __init__(self, *, trusted_module_prefixes: Iterable[str] = ("app.services.data_routing.adapters",)):
        self._trusted_prefixes = tuple(trusted_module_prefixes)
        self._adapters: dict[str, AdapterDefinition] = {}
        self._capabilities: dict[str, CapabilityDefinition] = {}
        self._frozen = False

    @property
    def adapters(self) -> Mapping[str, AdapterDefinition]:
        return dict(self._adapters)

    @property
    def capabilities(self) -> Mapping[str, CapabilityDefinition]:
        return dict(self._capabilities)

    def _assert_registration_open(self, source_module: str) -> None:
        if self._frozen:
            raise RegistryConflictError("registry is frozen")
        if not any(source_module == prefix or source_module.startswith(f"{prefix}.") for prefix in self._trusted_prefixes):
            raise RegistryValidationError(
                "registration source is outside trusted Adapter modules",
                details={"source_module": source_module},
            )

    def register_adapter(self, definition: AdapterDefinition) -> None:
        self._assert_registration_open(definition.source_module)
        _validate_key("adapter", definition.key)
        _validate_version("adapter", definition.version)
        _validate_object_schema("config_schema", definition.config_schema)
        _validate_object_schema("credential_schema", definition.credential_schema)
        if not definition.public_name.strip() or not definition.capabilities:
            raise RegistryValidationError("Adapter public_name and capabilities are required")
        if not isinstance(definition.quota_contract, Mapping) or not definition.quota_contract:
            raise RegistryValidationError("Adapter quota_contract is required")
        if not isinstance(definition.transport_retry_limit, int) or not 0 <= definition.transport_retry_limit <= 3:
            raise RegistryValidationError("Adapter transport_retry_limit must be between 0 and 3")
        for method in _REQUIRED_RUNTIME_METHODS:
            candidate = getattr(definition.runtime, method, None)
            if not callable(candidate):
                raise RegistryValidationError(f"Adapter runtime is missing {method}")
        for from_version, step in definition.config_migrations.items():
            if from_version != step.from_version or step.from_version == step.to_version or not callable(step.migrate):
                raise RegistryValidationError("invalid Adapter config migration step")
        if definition.key in self._adapters:
            raise RegistryConflictError("duplicate Adapter key", details={"key": definition.key})
        self._adapters[definition.key] = definition

    def register_capability(self, definition: CapabilityDefinition) -> None:
        self._assert_registration_open(definition.source_module)
        _validate_key("capability", definition.key)
        _validate_version("capability", definition.version)
        if not definition.public_name.strip() or not definition.semantic_family.strip():
            raise RegistryValidationError("Capability names and semantic family are required")
        if not definition.normalized_contract or not isinstance(definition.normalized_contract, Mapping):
            raise RegistryValidationError("Capability normalized_contract is required")
        if not definition.hard_quality_gates or not all(callable(gate) for gate in definition.hard_quality_gates):
            raise RegistryValidationError("Capability requires at least one hard quality gate")
        if not isinstance(definition.strict_profiles, Mapping) or not all(
            isinstance(name, str) and name and isinstance(thresholds, Mapping)
            for name, thresholds in definition.strict_profiles.items()
        ):
            raise RegistryValidationError("Capability strict_profiles must contain named threshold objects")
        if not definition.cache_key_fields or len(set(definition.cache_key_fields)) != len(definition.cache_key_fields):
            raise RegistryValidationError("Capability cache_key_fields must be unique and non-empty")
        if not set(definition.cache_key_fields).issubset(definition.allowed_constraints | {"subject"}):
            raise RegistryValidationError("Capability cache key uses undeclared constraint fields")
        if definition.key in self._capabilities:
            raise RegistryConflictError("duplicate Capability key", details={"key": definition.key})
        self._capabilities[definition.key] = definition

    def freeze(self) -> None:
        missing = sorted(
            (adapter.key, capability)
            for adapter in self._adapters.values()
            for capability in adapter.capabilities
            if capability not in self._capabilities
        )
        if missing:
            raise RegistryValidationError("Adapter references unknown Capability", details={"references": missing})
        self._frozen = True

    def adapter_fingerprint(self, key: str) -> str:
        definition = self._adapters[key]
        payload = {
            "key": definition.key,
            "version": definition.version,
            "public_name": definition.public_name,
            "config_schema": definition.config_schema,
            "credential_schema": definition.credential_schema,
            "capabilities": sorted(definition.capabilities),
            "quota_contract": definition.quota_contract,
            "transport_retry_limit": definition.transport_retry_limit,
            "runtime_type": f"{type(definition.runtime).__module__}.{type(definition.runtime).__qualname__}",
            "runtime_methods": {
                method: str(inspect.signature(getattr(definition.runtime, method))) for method in _REQUIRED_RUNTIME_METHODS
            },
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def capability_fingerprint(self, key: str) -> str:
        definition = self._capabilities[key]
        payload = asdict(definition)
        payload["allowed_constraints"] = sorted(definition.allowed_constraints)
        payload["hard_quality_gates"] = [
            f"{getattr(gate, '__module__', '')}.{getattr(gate, '__qualname__', type(gate).__qualname__)}"
            for gate in definition.hard_quality_gates
        ]
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


default_data_routing_registry = DataRoutingRegistry()
