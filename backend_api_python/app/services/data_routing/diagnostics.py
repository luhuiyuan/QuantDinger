"""Direct Provider Capability diagnostics without routing or circuit mutation."""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

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
    request_summary: Mapping[str, Any] = field(default_factory=dict)
    sample: Mapping[str, Any] | None = None
    duration_ms: int = 0


_SENSITIVE_FIELD_PARTS = frozenset(("secret", "credential", "password", "token", "api_key", "authorization", "cookie"))
_NON_PREVIEW_FIELD_NAMES = frozenset(("raw", "raw_payload", "request_context", "endpoint", "url", "traceback", "stack"))
_SAMPLE_ROW_LIMIT = 10
_SAMPLE_COLUMN_LIMIT = 16


def _safe_sample_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded JSON-safe value without credential-like fields."""
    if depth >= 4:
        return "[truncated]"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if is_dataclass(value):
        return _safe_sample_value(asdict(value), depth=depth)
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, Mapping):
        return {
            str(key): _safe_sample_value(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
            if str(key).lower() not in _NON_PREVIEW_FIELD_NAMES
            and not any(part in str(key).lower() for part in _SENSITIVE_FIELD_PARTS)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_sample_value(item, depth=depth + 1) for item in value[:30]]
    return str(value)[:512]


def _sample_preview(normalized: Any, *, kind: str) -> Mapping[str, Any]:
    """Shape normalized data into a small, provider-neutral admin preview."""
    if isinstance(normalized, Mapping) and isinstance(normalized.get("rows"), Sequence):
        source_rows = normalized["rows"]
    elif isinstance(normalized, Mapping) and isinstance(normalized.get("data"), Sequence):
        source_rows = normalized["data"]
    elif isinstance(normalized, Sequence) and not isinstance(normalized, (str, bytes, bytearray)):
        source_rows = normalized
    elif isinstance(getattr(normalized, "bars", None), Sequence):
        # DailyBarPage is the normalized history transport contract. Show its
        # bounded bars directly instead of serializing the page object repr.
        source_rows = normalized.bars
    else:
        source_rows = [normalized]
    rows = []
    columns = []
    for source in source_rows[:_SAMPLE_ROW_LIMIT]:
        safe = _safe_sample_value(source)
        row = safe if isinstance(safe, Mapping) else {"value": safe}
        rows.append(row)
        for key in row:
            if key not in columns and len(columns) < _SAMPLE_COLUMN_LIMIT:
                columns.append(key)
    return {
        "kind": kind,
        "columns": columns,
        "rows": rows,
        "truncated": len(source_rows) > _SAMPLE_ROW_LIMIT,
    }


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
        started_at = time.monotonic()
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
                    request_summary=_safe_sample_value({"subject": diagnostic_subject, "constraints": diagnostic_constraints}),
                    duration_ms=round((time.monotonic() - started_at) * 1000),
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
                request_summary=_safe_sample_value({"subject": diagnostic_subject, "constraints": diagnostic_constraints}),
                duration_ms=round((time.monotonic() - started_at) * 1000),
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
                _safe_sample_value({"subject": diagnostic_subject, "constraints": diagnostic_constraints}),
                _sample_preview(normalized, kind=capability.semantic_family),
                round((time.monotonic() - started_at) * 1000),
            )
        except Exception:
            result = ProviderDiagnosticResult(
                diagnostic_id, entry.instance_id, capability_key, False,
                "normalization_or_quality_error", ("normalization_or_quality_error",), (), None,
                request_summary=_safe_sample_value({"subject": diagnostic_subject, "constraints": diagnostic_constraints}),
                duration_ms=round((time.monotonic() - started_at) * 1000),
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
            "request_summary": dict(result.request_summary),
            "sample": dict(result.sample) if result.sample else None,
            "duration_ms": result.duration_ms,
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
                # An interactive capability diagnostic is verification evidence
                # for that capability.  It must not change circuit state (that
                # remains governed by routed requests and recovery probes), but
                # a successful diagnostic should clear stale transient
                # verification evidence so an operator can make the instance
                # eligible without replacing otherwise valid credentials.
                if bool(result.get("succeeded")):
                    cur.execute(
                        """UPDATE qd_provider_instance_capabilities capability
                           SET eligibility_status='eligible',
                               verification_evidence=jsonb_build_object(
                                   'code','diagnostic_succeeded',
                                   'status','eligible',
                                   'diagnostic_id',%s
                               ),
                               last_verified_at=NOW(),
                               next_verification_at=NOW()+INTERVAL '7 days',
                               updated_at=NOW()
                           FROM qd_provider_diagnostics diagnostic
                           WHERE diagnostic.diagnostic_id=%s
                             AND capability.instance_id=diagnostic.instance_id
                             AND capability.capability_key=diagnostic.capability_key
                             AND capability.eligibility_status<>'disabled'""",
                        (diagnostic_id, diagnostic_id),
                    )
                db.commit()
            finally:
                cur.close()

    def latest_for_instance(self, instance_id: int) -> list[dict[str, Any]]:
        """Return the latest completed, sanitized diagnostic for each capability."""
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT DISTINCT ON (capability_key)
                              diagnostic_id,instance_id,capability_key,status,sanitized_result,created_at,completed_at
                       FROM qd_provider_diagnostics
                       WHERE instance_id=%s AND status IN ('succeeded','failed')
                       ORDER BY capability_key,completed_at DESC NULLS LAST,created_at DESC,diagnostic_id DESC""",
                    (int(instance_id),),
                )
                rows = cur.fetchall() or []
            finally:
                cur.close()
        allowed = {
            "succeeded", "error_category", "quality_failures", "warnings", "acquired_at",
            "request_summary", "sample", "duration_ms",
        }
        items = []
        for row in rows:
            value = dict(row)
            raw_result = value.pop("sanitized_result", {})
            if isinstance(raw_result, str):
                try:
                    raw_result = json.loads(raw_result)
                except ValueError:
                    raw_result = {}
            result = dict(raw_result) if isinstance(raw_result, Mapping) else {}
            items.append({
                "diagnostic_id": value["diagnostic_id"],
                "instance_id": value["instance_id"],
                "capability_key": value["capability_key"],
                "status": value["status"],
                "created_at": value["created_at"],
                "completed_at": value["completed_at"],
                **{key: result[key] for key in allowed if key in result},
            })
        return items
