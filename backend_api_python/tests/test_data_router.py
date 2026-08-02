from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.services.data_routing.cache import RoutingCache, routing_cache_key
from app.services.data_routing.models import (
    AdapterDefinition,
    AdapterErrorClassification,
    AdapterFetchResult,
    CapabilityDefinition,
)
from app.services.data_routing.policy import RoutingPolicyError, RoutingPolicyService
from app.services.data_routing.registry import DataRoutingRegistry
from app.services.data_routing.router import (
    DataRequestMode,
    DataRouter,
    RoutedDataRequestError,
    RoutedDataUnavailableError,
)
from app.services.data_routing.snapshot import (
    AttemptEligibility,
    RoutingSnapshotManager,
    SnapshotEntrySource,
    SnapshotPolicySource,
    SnapshotVersionToken,
)


OBJECT_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


class TransportError(RuntimeError):
    pass


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class Runtime:
    def __init__(self, clock, actions, *, supports=True):
        self.clock = clock
        self.actions = list(actions)
        self.supports = supports
        self.calls = 0

    def resolve_account_identity(self, credentials, config): return "account"
    def diagnose(self, capability_key, subject, config, credentials): return {"ok": True}
    def normalize(self, capability_key, payload): return payload
    def estimate_quota_cost(self, capability_key, operation, units): return {"requests": units}
    def supports_constraints(self, capability_key, constraints, config): return self.supports

    def fetch(self, capability_key, subject, constraints, config, credentials, deadline):
        assert credentials == {"token": f"secret-{config['provider']}"}
        self.calls += 1
        action = self.actions.pop(0)
        if callable(action):
            action()
        if isinstance(action, Exception):
            raise action
        if isinstance(action, AdapterFetchResult):
            return action
        return AdapterFetchResult(action, self.clock())

    def classify_error(self, capability_key, error):
        return AdapterErrorClassification(
            "transport_timeout" if isinstance(error, TransportError) else "provider_error",
            isinstance(error, TransportError),
            retry_delay_seconds=0,
        )


def hard_gate(data):
    if not isinstance(data, dict):
        return ["not_an_object"]
    return [] if data.get("price", 0) > 0 else ["price_not_positive"]


class SnapshotRepo:
    def __init__(self, entries, *, quality_profile=None):
        self.token = SnapshotVersionToken(1, 1, 1, 1, 1, 1)
        self.entries = entries
        self.policy = SnapshotPolicySource(1, "quote", 1, True, "", 10, quality_profile or {})
        self.gates = {entry.instance_id: AttemptEligibility(True) for entry in entries}

    def current_version_token(self): return self.token
    def load_snapshot_sources(self): return self.token, (self.policy,), tuple(self.entries)
    def check_attempt_eligibility(self, instance_id, capability_key): return self.gates[instance_id]


class Resolver:
    @contextmanager
    def resolve(self, handle):
        yield {"token": f"secret-{handle.resolve_credential_id() - 100}"}


class BrokenSink:
    def request_started(self, **values): raise RuntimeError("logging unavailable")
    def attempt_recorded(self, attempt): raise RuntimeError("logging unavailable")
    def request_completed(self, **values): raise RuntimeError("logging unavailable")


class CacheStore:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key)
    def set(self, key, value, ttl): self.values[key] = dict(value)


def build_router(
    runtime_specs, *, requirements=None, quality_profile=None, entry_profiles=None,
    sink=None, cache=None, freshness_contract=None, quota=None, quota_contracts=None,
):
    clock = next(iter(runtime_specs.values()))[0].clock
    registry = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    registry.register_capability(CapabilityDefinition(
        "quote", "1", "Quote", "quote", "US", "tests.adapters.quote",
        frozenset({"venue", "adjustment", "max_delay"}), {"type": "quote"}, (hard_gate,),
        {"strict": {"required_fields": ["price", "symbol"], "max_age_seconds": 60}},
        ("subject", "venue", "adjustment"),
        freshness_contract or {"fresh_seconds": 30, "stale_seconds": 300},
    ))
    entries = []
    for position, (key, (runtime, retries)) in enumerate(runtime_specs.items(), start=1):
        registry.register_adapter(AdapterDefinition(
            key, "1", key.title(), f"tests.adapters.{key}", OBJECT_SCHEMA, OBJECT_SCHEMA,
            frozenset({"quote"}),
            (quota_contracts or {}).get(key, {"buckets": ["requests"]}), runtime,
            transport_retry_limit=retries,
        ))
        entries.append(SnapshotEntrySource(
            "quote", 1, 1, 10, position, position, f"instance_{position}", key, key.title(),
            "active", "eligible", {"provider": position},
            dict((requirements or {}).get(key, {})), dict((entry_profiles or {}).get(key, {})), 100 + position,
        ))
    registry.freeze()
    repository = SnapshotRepo(entries, quality_profile=quality_profile)
    snapshots = RoutingSnapshotManager(repository)
    snapshots.refresh_if_changed(force=True)
    return DataRouter(
        registry, snapshots, Resolver(), observability=sink, cache=cache, quota=quota, clock=clock
    ), repository


def test_constraints_are_whitelisted_and_filter_without_relaxation():
    clock = Clock()
    first = Runtime(clock, [{"price": 1, "symbol": "A"}])
    second = Runtime(clock, [{"price": 2, "symbol": "A"}])
    router, _ = build_router(
        {"first": (first, 0), "second": (second, 0)},
        requirements={"first": {"adjustment": ["raw"]}, "second": {"adjustment": ["split"]}},
    )

    with pytest.raises(RoutedDataRequestError) as caught:
        router.execute("quote", {"symbol": "A"}, {"administrator_expression": "true"}, calling_feature="quotes.api")
    assert caught.value.details["constraints"] == ["administrator_expression"]

    result = router.execute("quote", {"symbol": "A"}, {"adjustment": "split"}, calling_feature="quotes.api")
    uuid.UUID(result.routed_request_id)
    assert first.calls == 0 and second.calls == 1
    assert [item.skip_reason for item in result.provenance.attempts] == ["constraints_unsupported", ""]
    assert result.provenance.policy_revision_id == 10


def test_transport_retries_stay_in_one_attempt_before_ordered_fallback():
    clock = Clock()
    first = Runtime(clock, [TransportError("one"), TransportError("two")])
    second = Runtime(clock, [{"price": 3, "symbol": "A"}])
    router, _ = build_router({"first": (first, 1), "second": (second, 0)})

    result = router.execute("quote", {"symbol": "A"}, calling_feature="quotes.api")

    assert [item.outcome for item in result.provenance.attempts] == ["failed", "accepted"]
    summary = result.provenance.attempts[0].retry_summary
    assert summary.transport_calls == 2 and summary.retries == 1
    assert summary.categories == ("transport_timeout", "transport_timeout")


def test_hard_quality_failure_falls_back_but_warning_is_accepted_immediately():
    clock = Clock()
    rejected = Runtime(clock, [{"price": -1, "symbol": "A"}])
    warning = {"code": "partial_session", "message": "Current session is incomplete", "details": {}}
    accepted = Runtime(clock, [AdapterFetchResult({"price": 4, "symbol": "A"}, clock(), (warning,))])
    unused = Runtime(clock, [{"price": 5, "symbol": "A"}])
    router, _ = build_router(
        {"rejected": (rejected, 0), "accepted": (accepted, 0), "unused": (unused, 0)},
        quality_profile={"strict": True},
        sink=BrokenSink(),
    )

    result = router.execute("quote", {"symbol": "A"}, mode=DataRequestMode.INTERACTIVE, calling_feature="quotes.api")

    assert [item.outcome for item in result.provenance.attempts] == ["rejected", "accepted"]
    assert unused.calls == 0
    assert result.quality_warnings[0].code == "partial_session"
    assert result.freshness == "fresh" and result.provider_public_name == "Accepted"
    assert result.provenance.explanation[-1] == "attempt_2:accepted"


def test_live_gate_and_deadline_create_ordered_skips_without_provider_calls():
    clock = Clock()
    first = Runtime(clock, [{"price": 1, "symbol": "A"}])
    second = Runtime(clock, [{"price": 2, "symbol": "A"}])
    router, repository = build_router({"first": (first, 0), "second": (second, 0)})
    repository.gates[1] = AttemptEligibility(False, "quarantined")
    clock.value += timedelta(seconds=2)
    original_check = repository.check_attempt_eligibility

    def check_and_consume_budget(instance_id, capability_key):
        decision = original_check(instance_id, capability_key)
        if instance_id == 1:
            clock.value += timedelta(seconds=1)
        return decision

    repository.check_attempt_eligibility = check_and_consume_budget

    with pytest.raises(RoutedDataUnavailableError) as caught:
        router.execute(
            "quote", {"symbol": "A"}, calling_feature="quotes.api",
            deadline=clock.value + timedelta(milliseconds=1),
        )

    assert first.calls == second.calls == 0
    assert [item.skip_reason for item in caught.value.attempts] == ["quarantined", "deadline_exceeded"]
    assert caught.value.details["explanation"][-1] == "attempt_2:deadline_exceeded"


def test_all_constraint_candidates_skipped_returns_structured_failure():
    clock = Clock()
    runtime = Runtime(clock, [{"price": 1, "symbol": "A"}], supports=False)
    router, _ = build_router({"only": (runtime, 0)})

    with pytest.raises(RoutedDataUnavailableError) as caught:
        router.execute("quote", {"symbol": "A"}, {"venue": "NYSE"}, calling_feature="quotes.api")

    assert caught.value.code == "routed_data_unavailable"
    assert caught.value.attempts[0].skip_reason == "constraints_unsupported"
    assert runtime.calls == 0


class PolicyRepo:
    def save_draft(self, **values): return values


def test_policy_rejects_quality_threshold_relaxation_before_publish():
    clock = Clock()
    runtime = Runtime(clock, [])
    router, _ = build_router({"only": (runtime, 0)})
    service = RoutingPolicyService(router.registry, PolicyRepo())

    with pytest.raises(RoutingPolicyError, match="relax"):
        service.save_draft(
            "quote", [{"instance_id": 1}],
            quality_profile={"strict": {"max_age_seconds": 120}},
        )


def test_routing_cache_key_isolates_semantics_and_mode():
    clock = Clock()
    runtime = Runtime(clock, [])
    router, _ = build_router({"only": (runtime, 0)})
    capability = router.registry.capabilities["quote"]

    base = routing_cache_key(capability, {"symbol": "A"}, {"venue": "NYSE"}, "interactive")
    assert base != routing_cache_key(capability, {"symbol": "B"}, {"venue": "NYSE"}, "interactive")
    assert base != routing_cache_key(capability, {"symbol": "A"}, {"venue": "NASDAQ"}, "interactive")
    assert base != routing_cache_key(capability, {"symbol": "A"}, {"venue": "NYSE"}, "backtest")


def test_fresh_cache_returns_original_provenance_with_zero_attempts():
    clock = Clock()
    runtime = Runtime(clock, [{"price": 7, "symbol": "A"}])
    cache = RoutingCache(CacheStore())
    router, _ = build_router({"only": (runtime, 0)}, cache=cache)

    provider = router.execute("quote", {"symbol": "A"}, calling_feature="quotes.api")
    cached = router.execute("quote", {"symbol": "A"}, calling_feature="quotes.api")

    assert runtime.calls == 1
    assert provider.provenance.source_type == "provider"
    assert cached.freshness == "fresh" and cached.provenance.source_type == "routing_cache"
    assert cached.provenance.attempts == () and cached.provenance.attempt_order == 0
    assert cached.provenance.provider_instance_id == provider.provenance.provider_instance_id
    assert cached.provenance.content_digest == provider.provenance.content_digest


def test_provider_failure_falls_back_to_eligible_stale_cache_without_relaxing_constraints():
    clock = Clock()
    runtime = Runtime(clock, [{"price": 8, "symbol": "A"}, TransportError("down")])
    cache = RoutingCache(CacheStore())
    router, _ = build_router({"only": (runtime, 0)}, cache=cache)
    router.execute("quote", {"symbol": "A"}, {"venue": "NYSE"}, calling_feature="quotes.api")
    clock.value += timedelta(seconds=31)

    stale = router.execute("quote", {"symbol": "A"}, {"venue": "NYSE"}, calling_feature="quotes.api")
    assert stale.freshness == "stale"
    assert len(stale.provenance.attempts) == 1
    assert stale.provenance.attempts[0].outcome == "failed"
    assert runtime.calls == 2

    with pytest.raises(RoutedDataUnavailableError):
        router.execute("quote", {"symbol": "A"}, {"venue": "NASDAQ"}, calling_feature="quotes.api")


def test_disabled_capability_can_serve_cache_but_creates_no_provider_attempt():
    clock = Clock()
    runtime = Runtime(clock, [{"price": 9, "symbol": "A"}])
    cache = RoutingCache(CacheStore())
    router, repository = build_router({"only": (runtime, 0)}, cache=cache)
    router.execute("quote", {"symbol": "A"}, calling_feature="quotes.api")
    repository.policy = SnapshotPolicySource(1, "quote", 2, False, "maintenance", 10, {})
    repository.token = SnapshotVersionToken(2, 2, 1, 1, 1, 1)
    router.snapshots.refresh_if_changed()

    cached = router.execute("quote", {"symbol": "A"}, calling_feature="quotes.api")

    assert cached.freshness == "fresh" and cached.provenance.attempts == ()
    assert runtime.calls == 1


def test_persistent_dataset_capability_is_excluded_from_routing_cache():
    clock = Clock()
    runtime = Runtime(clock, [{"price": 1, "symbol": "A"}, {"price": 2, "symbol": "A"}])
    store = CacheStore()
    router, _ = build_router(
        {"only": (runtime, 0)}, cache=RoutingCache(store),
        freshness_contract={
            "fresh_seconds": 30,
            "stale_seconds": 300,
            "storage_class": "persistent_dataset",
        },
    )

    router.execute("quote", {"symbol": "A"}, calling_feature="history.sync")
    router.execute("quote", {"symbol": "A"}, calling_feature="history.sync")

    assert runtime.calls == 2 and store.values == {}
