"""Execution plane for pinned, constrained Routed Data Requests."""

from __future__ import annotations

import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

from .cache import RoutingCache, RoutingCacheEntry, content_digest, routing_cache_key
from .errors import DataRoutingError
from .health import ProviderHealthManager
from .models import AdapterErrorClassification, AdapterFetchResult, JsonObject
from .quality import QualityOutcome, QualityWarning, evaluate_quality
from .quota import QuotaManager, QuotaReservation
from .registry import DataRoutingRegistry
from .snapshot import PinnedRoutingRevision, RoutingSnapshotEntry, RoutingSnapshotManager, SecretHandle


class DataRequestMode(str, Enum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    BACKTEST = "backtest"


class RoutedDataRequestError(DataRoutingError):
    code = "routed_data_request_invalid"


class RoutedDataUnavailableError(DataRoutingError):
    code = "routed_data_unavailable"

    def __init__(self, message: str, *, request_id: str, attempts: Sequence["ProviderAttemptRecord"], details=None):
        merged = {"routed_request_id": request_id, **dict(details or {})}
        super().__init__(message, details=merged)
        self.request_id = request_id
        self.attempts = tuple(attempts)


@dataclass(frozen=True, slots=True)
class RetrySummary:
    transport_calls: int
    retries: int
    categories: tuple[str, ...] = ()
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class ProviderAttemptRecord:
    routed_request_id: str
    attempt_order: int
    instance_id: int
    adapter_key: str
    outcome: str
    skip_reason: str = ""
    error_category: str = ""
    error_permanence: str = ""
    error_scope: str = ""
    quota_reservation_ids: tuple[str, ...] = ()
    retry_summary: RetrySummary = field(default_factory=lambda: RetrySummary(0, 0))
    quality_failures: tuple[str, ...] = ()
    warnings: tuple[QualityWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutedDataProvenance:
    capability_key: str
    mode: str
    calling_feature: str
    deadline: datetime
    constraints: Mapping[str, Any]
    source_type: str
    cache_key: str
    content_digest: str
    policy_id: int
    policy_revision_id: int
    provider_instance_id: int
    adapter_key: str
    attempt_order: int
    attempts: tuple[ProviderAttemptRecord, ...]
    explanation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutedDataResult:
    data: Any
    routed_request_id: str
    provider_public_name: str
    acquired_at: datetime
    freshness: str
    quality_warnings: tuple[QualityWarning, ...]
    provenance: RoutedDataProvenance


class SecretResolver(Protocol):
    def resolve(self, handle: SecretHandle) -> AbstractContextManager[JsonObject]: ...


class RouterObservabilitySink(Protocol):
    def request_started(self, **values: Any) -> None: ...
    def attempt_recorded(self, attempt: ProviderAttemptRecord) -> None: ...
    def request_completed(self, **values: Any) -> None: ...


class NullRouterObservabilitySink:
    def request_started(self, **values: Any) -> None: pass
    def attempt_recorded(self, attempt: ProviderAttemptRecord) -> None: pass
    def request_completed(self, **values: Any) -> None: pass


_DEFAULT_TIMEOUTS = {
    DataRequestMode.INTERACTIVE: 10.0,
    DataRequestMode.BACKGROUND: 60.0,
    DataRequestMode.BACKTEST: 30.0,
}


def _matches_requirement(requested: Any, supported: Any, key: str) -> bool:
    if isinstance(supported, (list, tuple, set, frozenset)):
        return requested in supported
    if key.startswith("max_") and isinstance(requested, (int, float)) and isinstance(supported, (int, float)):
        return requested <= supported
    if key.startswith("min_") and isinstance(requested, (int, float)) and isinstance(supported, (int, float)):
        return requested >= supported
    return requested == supported


class DataRouter:
    def __init__(
        self,
        registry: DataRoutingRegistry,
        snapshots: RoutingSnapshotManager,
        secret_resolver: SecretResolver,
        *,
        observability: RouterObservabilitySink | None = None,
        cache: RoutingCache | None = None,
        quota: QuotaManager | None = None,
        health: ProviderHealthManager | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.registry = registry
        self.snapshots = snapshots
        self.secret_resolver = secret_resolver
        self.observability = observability or NullRouterObservabilitySink()
        self.cache = cache
        self.quota = quota
        self.health = health
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep

    def _emit(self, method: str, *args: Any, **kwargs: Any) -> None:
        try:
            getattr(self.observability, method)(*args, **kwargs)
        except Exception:
            pass

    def _request(
        self,
        capability_key: str,
        subject: Mapping[str, Any],
        constraints: Mapping[str, Any],
        mode: DataRequestMode | str,
        calling_feature: str,
        deadline: datetime | None,
        timeout_seconds: float | None,
    ):
        capability = self.registry.capabilities.get(str(capability_key or ""))
        if capability is None:
            raise RoutedDataRequestError("Data Capability is not registered", details={"capability_key": capability_key})
        try:
            selected_mode = mode if isinstance(mode, DataRequestMode) else DataRequestMode(str(mode))
        except ValueError as exc:
            raise RoutedDataRequestError("Data Request Mode is invalid") from exc
        feature = str(calling_feature or "").strip()
        if not feature or len(feature) > 160:
            raise RoutedDataRequestError("Calling Feature is required and must not exceed 160 characters")
        if not isinstance(subject, Mapping) or not isinstance(constraints, Mapping):
            raise RoutedDataRequestError("subject and constraints must be objects")
        unknown = sorted(set(constraints) - set(capability.allowed_constraints))
        if unknown:
            raise RoutedDataRequestError(
                "Data Eligibility Constraint is not declared by the Capability",
                details={"constraints": unknown},
            )
        now = self.clock()
        if deadline is not None and timeout_seconds is not None:
            raise RoutedDataRequestError("deadline and timeout_seconds are mutually exclusive")
        timeout = _DEFAULT_TIMEOUTS[selected_mode] if timeout_seconds is None else float(timeout_seconds)
        final_deadline = deadline or now + timedelta(seconds=timeout)
        if final_deadline.tzinfo is None or final_deadline.utcoffset() is None or final_deadline <= now:
            raise RoutedDataRequestError("deadline must be a future timezone-aware datetime")
        return capability, selected_mode, feature, final_deadline

    @staticmethod
    def _entry_supports(entry: RoutingSnapshotEntry, constraints: Mapping[str, Any]) -> bool:
        return all(
            key not in constraints or _matches_requirement(constraints[key], supported, key)
            for key, supported in entry.eligibility_requirements.items()
        )

    def _record(self, attempts: list[ProviderAttemptRecord], attempt: ProviderAttemptRecord) -> None:
        attempts.append(attempt)
        self._emit("attempt_recorded", attempt)

    def _cached_result(
        self,
        *,
        entry: RoutingCacheEntry | None,
        cache_key: str,
        freshness: str,
        capability: Any,
        pinned: PinnedRoutingRevision,
        request_id: str,
        mode: DataRequestMode,
        feature: str,
        deadline: datetime,
        constraints: Mapping[str, Any],
        attempts: Sequence[ProviderAttemptRecord] = (),
        explanation: Sequence[str] = (),
    ) -> RoutedDataResult | None:
        if entry is None or entry.capability_key != capability.key:
            return None
        age = max(0.0, (self.clock() - entry.acquired_at).total_seconds())
        fresh_seconds = max(0, int(capability.freshness_contract.get("fresh_seconds") or 0))
        stale_seconds = max(fresh_seconds, int(capability.freshness_contract.get("stale_seconds") or 0))
        if freshness == "fresh" and age > fresh_seconds:
            return None
        if freshness == "stale" and (age <= fresh_seconds or age > stale_seconds):
            return None
        quality = evaluate_quality(
            capability,
            entry.data,
            entry.acquired_at,
            policy_profile=pinned.quality_profile,
            entry_profile={},
            warnings=entry.quality_warnings,
            now=self.clock(),
        )
        if not quality.passed:
            return None
        cache_explanation = tuple(explanation) or (f"pinned_policy_revision:{pinned.revision_id}",)
        cache_explanation = (*cache_explanation, f"{freshness}_cache:accepted")
        provenance = RoutedDataProvenance(
            capability.key, mode.value, feature, deadline, dict(constraints), "routing_cache",
            cache_key, entry.content_digest, pinned.policy_id, pinned.revision_id,
            entry.provider_instance_id, entry.adapter_key, 0, tuple(attempts), cache_explanation,
        )
        return RoutedDataResult(
            entry.data, request_id, entry.provider_public_name, entry.acquired_at,
            freshness, quality.warnings, provenance,
        )

    def execute(
        self,
        capability_key: str,
        subject: Mapping[str, Any],
        constraints: Mapping[str, Any] | None = None,
        *,
        mode: DataRequestMode | str = DataRequestMode.INTERACTIVE,
        calling_feature: str,
        deadline: datetime | None = None,
        timeout_seconds: float | None = None,
        routed_request_id: str | None = None,
        pinned_revision: PinnedRoutingRevision | None = None,
        fixed_instance_id: int | None = None,
    ) -> RoutedDataResult:
        constraints = dict(constraints or {})
        capability, selected_mode, feature, final_deadline = self._request(
            capability_key, subject, constraints, mode, calling_feature, deadline, timeout_seconds
        )
        try:
            request_id = str(uuid.UUID(routed_request_id)) if routed_request_id else str(uuid.uuid4())
        except (ValueError, AttributeError) as exc:
            raise RoutedDataRequestError("Routed Data Request ID must be a UUID") from exc
        pinned = pinned_revision or self.snapshots.pin(capability_key)
        if pinned.capability_key != capability_key:
            raise RoutedDataRequestError("Pinned routing revision belongs to another Capability")
        if fixed_instance_id is not None and not any(
            entry.instance_id == fixed_instance_id for entry in pinned.entries
        ):
            raise RoutedDataRequestError(
                "Fixed Provider Instance is not present in the pinned routing revision",
                details={"instance_id": fixed_instance_id, "policy_revision_id": pinned.revision_id},
            )
        attempts: list[ProviderAttemptRecord] = []
        explanation = [f"pinned_policy_revision:{pinned.revision_id}"]
        cache_key = routing_cache_key(capability, subject, constraints, selected_mode.value)
        self._emit(
            "request_started", routed_request_id=request_id, capability_key=capability_key,
            mode=selected_mode.value, calling_feature=feature, deadline=final_deadline,
            policy_revision_id=pinned.revision_id, constraints=constraints, subject=subject,
        )
        cache_entry = self.cache.get(cache_key) if self.cache and self.cache.enabled(capability) else None
        cached = self._cached_result(
            entry=cache_entry, cache_key=cache_key, freshness="fresh", capability=capability,
            pinned=pinned, request_id=request_id, mode=selected_mode, feature=feature,
            deadline=final_deadline, constraints=constraints,
            attempts=attempts, explanation=explanation,
        )
        if cached is not None:
            self._emit("request_completed", routed_request_id=request_id, outcome="fresh_cache", provider_instance_id=None, attempt_count=0, result=cached)
            return cached
        if not pinned.enabled:
            cached = self._cached_result(
                entry=cache_entry, cache_key=cache_key, freshness="stale", capability=capability,
                pinned=pinned, request_id=request_id, mode=selected_mode, feature=feature,
                deadline=final_deadline, constraints=constraints,
                attempts=attempts, explanation=explanation,
            )
            if cached is not None:
                self._emit("request_completed", routed_request_id=request_id, outcome="stale_cache", provider_instance_id=None, attempt_count=0, result=cached)
                return cached
            pinned.require_provider_attempts_enabled()

        for order, entry in enumerate(pinned.entries, start=1):
            if fixed_instance_id is not None and entry.instance_id != fixed_instance_id:
                continue
            adapter = self.registry.adapters.get(entry.adapter_key)
            if adapter is None or capability_key not in adapter.capabilities:
                attempt = ProviderAttemptRecord(request_id, order, entry.instance_id, entry.adapter_key, "skipped", "adapter_unavailable")
                self._record(attempts, attempt)
                explanation.append(f"attempt_{order}:adapter_unavailable")
                continue
            if self.clock() >= final_deadline:
                attempt = ProviderAttemptRecord(request_id, order, entry.instance_id, entry.adapter_key, "skipped", "deadline_exceeded")
                self._record(attempts, attempt)
                explanation.append(f"attempt_{order}:deadline_exceeded")
                continue
            try:
                constraints_supported = self._entry_supports(entry, constraints) and adapter.runtime.supports_constraints(
                    capability_key, constraints, entry.non_secret_config
                )
            except Exception:
                constraints_supported = False
            if not constraints_supported:
                attempt = ProviderAttemptRecord(request_id, order, entry.instance_id, entry.adapter_key, "skipped", "constraints_unsupported")
                self._record(attempts, attempt)
                explanation.append(f"attempt_{order}:constraints_unsupported")
                continue
            live = self.snapshots.check_before_attempt(pinned, entry)
            if not live.allowed:
                attempt = ProviderAttemptRecord(request_id, order, entry.instance_id, entry.adapter_key, "skipped", live.reason)
                self._record(attempts, attempt)
                explanation.append(f"attempt_{order}:{live.reason}")
                continue

            quota_reservation: QuotaReservation | None = None
            if self.quota is not None:
                quota_decision = self.quota.acquire(
                    adapter,
                    instance_id=entry.instance_id,
                    capability_key=capability_key,
                    holder_id=request_id,
                    mode=selected_mode.value,
                    deadline=final_deadline,
                    units=adapter.transport_retry_limit + 1,
                )
                if not quota_decision.allowed:
                    attempt = ProviderAttemptRecord(
                        request_id, order, entry.instance_id, entry.adapter_key,
                        "skipped", quota_decision.reason or "quota_exhausted",
                    )
                    self._record(attempts, attempt)
                    explanation.append(f"attempt_{order}:{attempt.skip_reason}")
                    continue
                quota_reservation = quota_decision.reservation

            categories: list[str] = []
            fetch_result: AdapterFetchResult | None = None
            classification: AdapterErrorClassification | None = None
            calls = 0
            try:
                with self.secret_resolver.resolve(entry.secret_handle) as credentials:
                    for call_index in range(adapter.transport_retry_limit + 1):
                        if self.clock() >= final_deadline:
                            break
                        calls += 1
                        try:
                            fetch_result = adapter.runtime.fetch(
                                capability_key, dict(subject), constraints, entry.non_secret_config,
                                credentials, final_deadline,
                            )
                            if not isinstance(fetch_result, AdapterFetchResult):
                                raise TypeError("Adapter fetch must return AdapterFetchResult")
                            break
                        except Exception as exc:
                            try:
                                classification = adapter.runtime.classify_error(capability_key, exc)
                            except Exception:
                                classification = AdapterErrorClassification("unclassified", False)
                            if not isinstance(classification, AdapterErrorClassification):
                                classification = AdapterErrorClassification("unclassified", False)
                            categories.append(str(classification.category or "unclassified")[:120])
                            if not classification.retryable or call_index >= adapter.transport_retry_limit:
                                break
                            delay = max(0.0, min(float(classification.retry_delay_seconds), 5.0))
                            remaining = (final_deadline - self.clock()).total_seconds()
                            if remaining <= delay:
                                break
                            if delay:
                                self.sleep(delay)
            except Exception:
                classification = AdapterErrorClassification(
                    "credential_unavailable", False, permanence="permanent", scope_hint="instance"
                )
                categories.append(classification.category)

            retries = max(0, calls - 1)
            retry_summary = RetrySummary(
                calls,
                retries,
                tuple(categories),
                bool(fetch_result is None and classification and classification.retryable and calls >= adapter.transport_retry_limit + 1),
            )
            if fetch_result is not None and self.clock() >= final_deadline:
                fetch_result = None
                classification = AdapterErrorClassification("deadline", False)
            if self.quota is not None:
                self.quota.complete(quota_reservation, actual_calls=calls)
                if fetch_result is not None:
                    self.quota.observe(
                        adapter,
                        instance_id=entry.instance_id,
                        capability_key=capability_key,
                        observations=fetch_result.quota_observations,
                    )
            if fetch_result is None:
                reason = "deadline_exceeded" if self.clock() >= final_deadline else "provider_error"
                attempt = ProviderAttemptRecord(
                    routed_request_id=request_id,
                    attempt_order=order,
                    instance_id=entry.instance_id,
                    adapter_key=entry.adapter_key,
                    outcome="failed",
                    skip_reason=reason,
                    error_category=str(classification.category if classification else "deadline"),
                    error_permanence=str(classification.permanence if classification else "transient"),
                    error_scope=str(classification.scope_hint if classification else "capability"),
                    quota_reservation_ids=quota_reservation.reservation_ids if quota_reservation else (),
                    retry_summary=retry_summary,
                )
                self._record(attempts, attempt)
                if self.health is not None and classification is not None:
                    try:
                        self.health.record_failure(
                            capability, instance_id=entry.instance_id,
                            classification=classification, routed_request_id=request_id,
                        )
                    except Exception:
                        pass
                explanation.append(f"attempt_{order}:{reason}")
                continue

            try:
                normalized = adapter.runtime.normalize(capability_key, fetch_result.payload)
                quality: QualityOutcome = evaluate_quality(
                    capability, normalized, fetch_result.acquired_at,
                    policy_profile=pinned.quality_profile,
                    entry_profile=entry.stricter_quality_profile,
                    warnings=fetch_result.warnings,
                    now=self.clock(),
                )
            except Exception:
                quality = QualityOutcome(False, ("normalization_or_quality_error",), (), ())
                normalized = None
            if not quality.passed:
                attempt = ProviderAttemptRecord(
                    request_id, order, entry.instance_id, entry.adapter_key, "rejected", "quality_gate_failed",
                    quota_reservation_ids=quota_reservation.reservation_ids if quota_reservation else (),
                    retry_summary=retry_summary, quality_failures=quality.hard_failures, warnings=quality.warnings,
                )
                self._record(attempts, attempt)
                if self.health is not None:
                    try:
                        self.health.record_failure(
                            capability, instance_id=entry.instance_id,
                            classification=AdapterErrorClassification(
                                "quality_gate_failed", True, permanence="transient", scope_hint="capability"
                            ),
                            routed_request_id=request_id,
                        )
                    except Exception:
                        pass
                explanation.append(f"attempt_{order}:quality_gate_failed")
                continue

            attempt = ProviderAttemptRecord(
                request_id, order, entry.instance_id, entry.adapter_key, "accepted",
                quota_reservation_ids=quota_reservation.reservation_ids if quota_reservation else (),
                retry_summary=retry_summary, warnings=quality.warnings,
            )
            self._record(attempts, attempt)
            if self.health is not None:
                try:
                    self.health.record_success(
                        capability_key, instance_id=entry.instance_id, routed_request_id=request_id
                    )
                except Exception:
                    pass
            explanation.append(f"attempt_{order}:accepted")
            provenance = RoutedDataProvenance(
                capability_key, selected_mode.value, feature, final_deadline, dict(constraints),
                "provider", cache_key, content_digest(normalized),
                pinned.policy_id, pinned.revision_id, entry.instance_id, entry.adapter_key,
                order, tuple(attempts), tuple(explanation),
            )
            result = RoutedDataResult(
                normalized, request_id, adapter.public_name, fetch_result.acquired_at,
                "fresh", quality.warnings, provenance,
            )
            if self.cache:
                self.cache.put(
                    key=cache_key, capability=capability, data=normalized,
                    provider_public_name=adapter.public_name, provider_instance_id=entry.instance_id,
                    adapter_key=entry.adapter_key, acquired_at=fetch_result.acquired_at,
                    quality_warnings=quality.warnings, quality_profiles=quality.applied_profiles,
                    now=self.clock(),
                )
            self._emit(
                "request_completed", routed_request_id=request_id, outcome="provider",
                provider_instance_id=entry.instance_id, attempt_count=len(attempts), result=result,
            )
            return result

        cached = self._cached_result(
            entry=cache_entry, cache_key=cache_key, freshness="stale", capability=capability,
            pinned=pinned, request_id=request_id, mode=selected_mode, feature=feature,
            deadline=final_deadline, constraints=constraints,
            attempts=attempts, explanation=explanation,
        )
        if cached is not None:
            self._emit(
                "request_completed", routed_request_id=request_id, outcome="stale_cache",
                provider_instance_id=None, attempt_count=len(attempts), result=cached,
            )
            return cached

        self._emit(
            "request_completed", routed_request_id=request_id, outcome="failed",
            provider_instance_id=None, attempt_count=len(attempts), result=None,
            explanation=tuple(explanation),
        )
        raise RoutedDataUnavailableError(
            "No eligible Provider returned data that passed hard quality gates",
            request_id=request_id,
            attempts=attempts,
            details={"policy_revision_id": pinned.revision_id, "explanation": explanation},
        )
