"""Write-only Provider credentials, identity checks, and Capability verification."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.utils.credential_crypto import (
    ProviderCredentialKeyring,
    decrypt_provider_credential_blob,
    encrypt_provider_credential_blob,
    provider_credential_comparison_tag,
)

from .errors import DataRoutingError
from .registry import DataRoutingRegistry


class ProviderCredentialError(DataRoutingError):
    code = "provider_credential_invalid"


class DuplicateProviderCredentialError(ProviderCredentialError):
    code = "provider_credential_duplicate"


class ProviderAccountConflictError(ProviderCredentialError):
    code = "provider_account_conflict"


class CrossAccountRotationError(ProviderCredentialError):
    code = "provider_credential_cross_account_rotation"


class ProviderCapabilityValidationError(ProviderCredentialError):
    code = "provider_capability_validation_failed"


@dataclass(frozen=True, slots=True)
class CredentialInstanceContext:
    instance_id: int
    adapter_key: str
    lifecycle_status: str
    provider_account_identity: str | None
    non_secret_config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StoredCredentialSecret:
    credential_id: int
    credential_version: int
    credential_schema_version: str
    status: str
    encryption_key_id: str
    ciphertext: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Safe administrative view; intentionally has no secret-bearing fields."""

    configured: bool
    active_version: int | None
    pending_version: int | None
    active_key_id: str | None
    pending_key_id: str | None
    active_updated_at: Any = None
    pending_updated_at: Any = None


@dataclass(frozen=True, slots=True)
class CapabilityVerification:
    capability_key: str
    outcome: str
    evidence: Mapping[str, Any]

    @property
    def eligible(self) -> bool:
        return self.outcome == "eligible"


class ProviderCredentialRepository(Protocol):
    def get_instance_context(self, instance_id: int) -> CredentialInstanceContext | None: ...
    def find_duplicate_secret_tag(self, secret_tag: str) -> int | None: ...
    def stage_pending_credential(self, *, instance_id: int, schema_version: str, key_id: str, ciphertext: str, secret_tag: str, actor_user_id: int | None, reason: str) -> StoredCredentialSecret: ...
    def destroy_pending_credential(self, credential_id: int, *, actor_user_id: int | None, reason: str) -> None: ...
    def get_active_credential(self, instance_id: int) -> StoredCredentialSecret | None: ...
    def record_pending_validation(self, credential_id: int, results: Sequence[CapabilityVerification], *, initial_credential: bool) -> None: ...
    def find_account_identity_conflict(self, adapter_key: str, identity: str, *, exclude_instance_id: int) -> int | None: ...
    def find_other_unidentified_active(self, adapter_key: str, *, exclude_instance_id: int) -> int | None: ...
    def activate_validated_pending(self, *, instance: CredentialInstanceContext, pending: StoredCredentialSecret, provider_account_identity: str | None, results: Sequence[CapabilityVerification], actor_user_id: int | None, reason: str) -> CredentialStatus: ...
    def get_credential_status(self, instance_id: int) -> CredentialStatus: ...
    def mark_capabilities_for_revalidation(self, instance_id: int, capability_keys: Sequence[str], *, trigger: str) -> None: ...
    def apply_capability_revalidation(self, instance_id: int, results: Sequence[CapabilityVerification], *, trigger: str) -> None: ...
    def disable_capability(self, instance_id: int, capability_key: str, *, actor_user_id: int | None, reason: str) -> None: ...
    def list_due_revalidation_instances(self, *, limit: int) -> Sequence[int]: ...


def _canonical_secret(credentials: Mapping[str, Any]) -> str:
    return json.dumps(dict(credentials), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_secret_schema(schema: Mapping[str, Any], credentials: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(credentials, Mapping):
        raise ProviderCredentialError("Provider credential bundle must be an object")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    unknown = sorted(set(credentials) - set(properties))
    if schema.get("additionalProperties", False) is False and unknown:
        raise ProviderCredentialError("Provider credential bundle contains undeclared fields", details={"fields": unknown})
    missing = sorted(name for name in required if name not in credentials or credentials[name] in (None, ""))
    if missing:
        raise ProviderCredentialError("Provider credential bundle is missing required fields", details={"fields": missing})
    for name, value in credentials.items():
        expected = (properties.get(name) or {}).get("type")
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if expected and not valid.get(expected, True):
            raise ProviderCredentialError("Provider credential field has invalid type", details={"field": name, "expected": expected})
    return dict(credentials)


def _safe_diagnostic_evidence(result: Any) -> Mapping[str, Any]:
    if isinstance(result, bool):
        return {"ok": result}
    if isinstance(result, Mapping):
        allowed = {"ok", "eligible", "permanent", "code", "plan", "region", "capability_version"}
        return {str(key): value for key, value in result.items() if key in allowed and isinstance(value, (str, int, float, bool, type(None)))}
    return {"ok": bool(result)}


class ProviderCredentialService:
    REVALIDATION_TRIGGERS = frozenset({"configuration_change", "credential_change", "permission_evidence", "periodic"})

    def __init__(
        self,
        registry: DataRoutingRegistry,
        repository: ProviderCredentialRepository,
        *,
        keyring: ProviderCredentialKeyring,
        comparison_pepper: str,
    ):
        self.registry = registry
        self.repository = repository
        self.keyring = keyring
        self.comparison_pepper = comparison_pepper

    def _instance_and_adapter(self, instance_id: int):
        instance = self.repository.get_instance_context(instance_id)
        if instance is None or instance.lifecycle_status == "retired":
            raise ProviderCredentialError("Provider Instance is unavailable", details={"instance_id": instance_id})
        adapter = self.registry.adapters.get(instance.adapter_key)
        if adapter is None:
            raise ProviderCredentialError("Provider Adapter is unavailable", details={"adapter_key": instance.adapter_key})
        return instance, adapter

    @staticmethod
    def _verification_from_result(capability_key: str, raw: Any) -> CapabilityVerification:
        evidence = _safe_diagnostic_evidence(raw)
        ok = bool(evidence.get("ok", evidence.get("eligible", False)))
        if ok:
            outcome = "eligible"
        elif evidence.get("permanent"):
            outcome = "ineligible"
        else:
            outcome = "transient_failure"
        return CapabilityVerification(capability_key, outcome, evidence)

    def _verify_capabilities(self, adapter, instance, credentials, capability_keys) -> tuple[CapabilityVerification, ...]:
        selected = tuple(sorted(set(capability_keys or adapter.capabilities)))
        unsupported = sorted(set(selected) - set(adapter.capabilities))
        if unsupported:
            raise ProviderCapabilityValidationError("Adapter does not declare requested Capability", details={"capabilities": unsupported})
        output = []
        for capability_key in selected:
            try:
                raw = adapter.runtime.diagnose(capability_key, {"operation": "credential_validation"}, dict(instance.non_secret_config), credentials)
                verified = self._verification_from_result(capability_key, raw)
                evidence = {**dict(verified.evidence), "capability_version": self.registry.capabilities[capability_key].version}
                output.append(CapabilityVerification(verified.capability_key, verified.outcome, evidence))
            except Exception as exc:
                output.append(CapabilityVerification(capability_key, "transient_failure", {"code": type(exc).__name__}))
        return tuple(output)

    def submit_and_validate(
        self,
        instance_id: int,
        credentials: Mapping[str, Any],
        *,
        actor_user_id: int | None,
        reason: str,
        capability_keys: Sequence[str] | None = None,
    ) -> CredentialStatus:
        if not str(reason or "").strip():
            raise ProviderCredentialError("credential change reason is required")
        instance, adapter = self._instance_and_adapter(instance_id)
        secret = _validate_secret_schema(adapter.credential_schema, credentials)
        canonical = _canonical_secret(secret)
        if secret:
            secret_tag = provider_credential_comparison_tag(canonical, pepper=self.comparison_pepper)
            duplicate = self.repository.find_duplicate_secret_tag(secret_tag)
            if duplicate is not None:
                raise DuplicateProviderCredentialError("Provider credential bundle is already in use")
        else:
            # Credentialless public Adapters still need an encrypted active
            # credential row so snapshots have one uniform SecretHandle path.
            # An empty object is not a reusable secret, so give each version a
            # non-comparable unique marker instead of globally deduplicating
            # every public Provider Instance against the same "{}" payload.
            secret_tag = f"credentialless:{uuid.uuid4().hex}"
        encrypted = encrypt_provider_credential_blob(canonical, keyring=self.keyring)
        pending = self.repository.stage_pending_credential(
            instance_id=instance_id,
            schema_version=adapter.version,
            key_id=encrypted.key_id,
            ciphertext=encrypted.ciphertext,
            secret_tag=secret_tag,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        active = self.repository.get_active_credential(instance_id)
        try:
            identity = adapter.runtime.resolve_account_identity(secret, dict(instance.non_secret_config))
            identity = str(identity).strip() if identity is not None else None
        except Exception as exc:
            self.repository.record_pending_validation(
                pending.credential_id,
                (CapabilityVerification("account_identity", "transient_failure", {"code": type(exc).__name__}),),
                initial_credential=active is None,
            )
            raise ProviderCredentialError("Provider account identity validation failed") from exc

        if active is not None and instance.provider_account_identity != identity:
            self.repository.destroy_pending_credential(pending.credential_id, actor_user_id=actor_user_id, reason="cross_account_rotation_rejected")
            raise CrossAccountRotationError("replacement credentials belong to a different Provider account; create a new Provider Instance")
        if identity is not None:
            conflict = self.repository.find_account_identity_conflict(instance.adapter_key, identity, exclude_instance_id=instance_id)
            if conflict is not None:
                self.repository.destroy_pending_credential(pending.credential_id, actor_user_id=actor_user_id, reason="duplicate_provider_account")
                raise ProviderAccountConflictError("Provider account already belongs to another non-retired Provider Instance")
        elif self.repository.find_other_unidentified_active(instance.adapter_key, exclude_instance_id=instance_id) is not None:
            self.repository.destroy_pending_credential(pending.credential_id, actor_user_id=actor_user_id, reason="unidentified_adapter_single_instance_limit")
            raise ProviderAccountConflictError("Adapter cannot identify accounts and already has an active Provider Instance")

        if capability_keys is not None and not set(capability_keys).issubset(adapter.capabilities):
            raise ProviderCapabilityValidationError("Adapter does not declare requested Capability")
        # A credential version is shared by every Capability on the Instance.
        # Activating it requires fresh evidence for the complete declared set.
        results = self._verify_capabilities(adapter, instance, secret, adapter.capabilities)
        if not any(result.eligible for result in results):
            self.repository.record_pending_validation(pending.credential_id, results, initial_credential=active is None)
            raise ProviderCapabilityValidationError("no Provider Capability passed validation")
        return self.repository.activate_validated_pending(
            instance=instance,
            pending=pending,
            provider_account_identity=identity,
            results=results,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    def get_status(self, instance_id: int) -> CredentialStatus:
        self._instance_and_adapter(instance_id)
        return self.repository.get_credential_status(instance_id)

    def request_revalidation(self, instance_id: int, *, trigger: str, capability_keys: Sequence[str] | None = None) -> None:
        if trigger not in self.REVALIDATION_TRIGGERS:
            raise ProviderCredentialError("invalid Capability revalidation trigger")
        instance, adapter = self._instance_and_adapter(instance_id)
        selected = tuple(sorted(set(capability_keys or adapter.capabilities)))
        if not set(selected).issubset(adapter.capabilities):
            raise ProviderCapabilityValidationError("Adapter does not declare requested Capability")
        self.repository.mark_capabilities_for_revalidation(instance.instance_id, selected, trigger=trigger)

    def run_revalidation(self, instance_id: int, *, trigger: str, capability_keys: Sequence[str] | None = None) -> tuple[CapabilityVerification, ...]:
        if trigger not in self.REVALIDATION_TRIGGERS:
            raise ProviderCredentialError("invalid Capability revalidation trigger")
        instance, adapter = self._instance_and_adapter(instance_id)
        active = self.repository.get_active_credential(instance_id)
        if active is None:
            raise ProviderCredentialError("Provider Instance has no active credentials")
        plaintext = decrypt_provider_credential_blob(active.ciphertext, key_id=active.encryption_key_id, keyring=self.keyring)
        secret = _validate_secret_schema(adapter.credential_schema, json.loads(plaintext))
        results = self._verify_capabilities(adapter, instance, secret, capability_keys)
        self.repository.apply_capability_revalidation(instance_id, results, trigger=trigger)
        return results

    def disable_capability(self, instance_id: int, capability_key: str, *, actor_user_id: int | None, reason: str) -> None:
        if not str(reason or "").strip():
            raise ProviderCredentialError("Capability disable reason is required")
        instance, adapter = self._instance_and_adapter(instance_id)
        if capability_key not in adapter.capabilities:
            raise ProviderCapabilityValidationError("Adapter does not declare requested Capability")
        self.repository.disable_capability(instance.instance_id, capability_key, actor_user_id=actor_user_id, reason=reason)

    def revalidate_due(self, *, limit: int = 10) -> dict[int, tuple[CapabilityVerification, ...]]:
        bounded = max(1, min(int(limit), 50))
        return {
            instance_id: self.run_revalidation(instance_id, trigger="periodic")
            for instance_id in self.repository.list_due_revalidation_instances(limit=bounded)
        }
