"""Direct Provider Capability diagnostics without routing or circuit mutation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from .errors import DataRoutingError
from .models import AdapterErrorClassification, AdapterFetchResult
from .quality import evaluate_quality
from .quota import QuotaManager
from .registry import DataRoutingRegistry
from .router import SecretResolver
from .snapshot import RoutingSnapshotEntry


class ProviderDiagnosticError(DataRoutingError):
    code = "provider_diagnostic_failed"


@dataclass(frozen=True, slots=True)
class ProviderDiagnosticResult:
    diagnostic_id: str
    instance_id: int
    capability_key: str
    succeeded: bool
    error_category: str
    quality_failures: tuple[str, ...]
    warnings: tuple[Mapping[str, Any], ...]
    acquired_at: datetime | None


class DiagnosticRepository(Protocol):
    def start(self, **values: Any) -> None: ...
    def complete(self, diagnostic_id: str, *, status: str, result: Mapping[str, Any]) -> None: ...


class ProviderCapabilityTestService:
    def __init__(
        self,
        registry: DataRoutingRegistry,
        secret_resolver: SecretResolver,
        repository: DiagnosticRepository,
        *,
        quota: QuotaManager | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.registry = registry
        self.secret_resolver = secret_resolver
        self.repository = repository
        self.quota = quota
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        entry: RoutingSnapshotEntry,
        *,
        capability_key: str,
        subject: Mapping[str, Any],
        constraints: Mapping[str, Any],
        requested_by: int | None,
        timeout_seconds: float = 10,
    ) -> ProviderDiagnosticResult:
        capability = self.registry.capabilities.get(capability_key)
        adapter = self.registry.adapters.get(entry.adapter_key)
        if capability is None or adapter is None or capability_key not in adapter.capabilities:
            raise ProviderDiagnosticError("Diagnostic target is not registered")
        unknown = sorted(set(constraints) - set(capability.allowed_constraints))
        if unknown or not adapter.runtime.supports_constraints(capability_key, constraints, entry.non_secret_config):
            raise ProviderDiagnosticError("Diagnostic constraints are unsupported", details={"constraints": unknown})
        diagnostic_subject = dict(subject)
        diagnostic_constraints = dict(constraints)
        sample = getattr(adapter.runtime, "diagnostic_request", None)
        if not diagnostic_subject and not diagnostic_constraints and callable(sample):
            diagnostic_subject, diagnostic_constraints = sample(capability_key)
        diagnostic_id = uuid.uuid4().hex
        deadline = self.clock() + timedelta(seconds=max(1, min(float(timeout_seconds), 60)))
        self.repository.start(
            diagnostic_id=diagnostic_id, instance_id=entry.instance_id,
            capability_key=capability_key, requested_by=requested_by,
        )
        reservation = None
        if self.quota is not None:
            decision = self.quota.acquire(
                adapter, instance_id=entry.instance_id, capability_key=capability_key,
                holder_id=diagnostic_id, mode="interactive", deadline=deadline,
                units=adapter.transport_retry_limit + 1, purpose="diagnostic",
            )
            if not decision.allowed:
                result = ProviderDiagnosticResult(
                    diagnostic_id, entry.instance_id, capability_key, False,
                    decision.reason or "quota_exhausted", (), (), None,
                )
                self.repository.complete(diagnostic_id, status="failed", result=self._safe(result))
                return result
            reservation = decision.reservation
        calls = 0
        fetch_result = None
        classification = AdapterErrorClassification("provider_error", False)
        try:
            with self.secret_resolver.resolve(entry.secret_handle) as credentials:
                for index in range(adapter.transport_retry_limit + 1):
                    if self.clock() >= deadline:
                        classification = AdapterErrorClassification("deadline", False)
                        break
                    calls += 1
                    try:
                        fetch_result = adapter.runtime.fetch(
                            capability_key, diagnostic_subject, diagnostic_constraints,
                            entry.non_secret_config, credentials, deadline,
                        )
                        if not isinstance(fetch_result, AdapterFetchResult):
                            raise TypeError("Adapter fetch result is invalid")
                        break
                    except Exception as exc:
                        try:
                            classification = adapter.runtime.classify_error(capability_key, exc)
                        except Exception:
                            classification = AdapterErrorClassification("unclassified", False)
                        if not classification.retryable or index >= adapter.transport_retry_limit:
                            break
        except Exception:
            classification = AdapterErrorClassification("credential_unavailable", False, "permanent", "instance")
        finally:
            if self.quota is not None:
                self.quota.complete(reservation, actual_calls=calls)
        if fetch_result is None:
            result = ProviderDiagnosticResult(
                diagnostic_id, entry.instance_id, capability_key, False,
                classification.category, (), (), None,
            )
            self.repository.complete(diagnostic_id, status="failed", result=self._safe(result))
            return result
        try:
            normalized = adapter.runtime.normalize(capability_key, fetch_result.payload)
            quality = evaluate_quality(
                capability, normalized, fetch_result.acquired_at,
                policy_profile={}, entry_profile=entry.stricter_quality_profile,
                warnings=fetch_result.warnings, now=self.clock(),
            )
            result = ProviderDiagnosticResult(
                diagnostic_id, entry.instance_id, capability_key, quality.passed,
                "" if quality.passed else "quality_gate_failed", quality.hard_failures,
                tuple({"code": item.code, "message": item.message} for item in quality.warnings),
                fetch_result.acquired_at,
            )
        except Exception:
            result = ProviderDiagnosticResult(
                diagnostic_id, entry.instance_id, capability_key, False,
                "normalization_or_quality_error", ("normalization_or_quality_error",), (), None,
            )
        self.repository.complete(
            diagnostic_id, status="succeeded" if result.succeeded else "failed", result=self._safe(result)
        )
        return result

    @staticmethod
    def _safe(result: ProviderDiagnosticResult) -> dict[str, Any]:
        return {
            "succeeded": result.succeeded,
            "error_category": result.error_category[:120],
            "quality_failures": list(result.quality_failures)[:50],
            "warnings": list(result.warnings)[:50],
            "acquired_at": result.acquired_at.isoformat() if result.acquired_at else None,
        }


class PostgresDiagnosticRepository:
    def __init__(self, connection_factory=None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    def start(self, **values) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_provider_diagnostics
                       (diagnostic_id,instance_id,capability_key,requested_by,status)
                       VALUES (%s,%s,%s,%s,'running')""",
                    (
                        values["diagnostic_id"], values["instance_id"],
                        values["capability_key"], values["requested_by"],
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def complete(self, diagnostic_id: str, *, status: str, result: Mapping[str, Any]) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_diagnostics SET status=%s,sanitized_result=%s::jsonb,completed_at=NOW()
                       WHERE diagnostic_id=%s AND status IN ('queued','running')""",
                    (status, json.dumps(dict(result), sort_keys=True), diagnostic_id),
                )
                db.commit()
            finally:
                cur.close()
