from __future__ import annotations

from datetime import timedelta

from app.services.data_routing.models import AdapterDefinition
from app.services.data_routing.quota import (
    QuotaDecision,
    QuotaManager,
    QuotaReservation,
    SafetySlice,
    SafetySlicePool,
    quota_bucket_contracts,
)
from tests.test_data_router import Clock, OBJECT_SCHEMA, Runtime, build_router


def adapter(runtime, *, limit=2, safety=None):
    contract = {
        "unit": "request",
        "scope": "capability",
        "configured_limit": limit,
    }
    if limit is None:
        contract.pop("configured_limit")
    if safety is not None:
        contract["unknown_safety_limit"] = safety
    return AdapterDefinition(
        "quota_adapter", "1", "Quota", "tests.adapters.quota", OBJECT_SCHEMA, OBJECT_SCHEMA,
        frozenset({"quote"}), {"buckets": {"requests": contract}}, runtime,
    )


class Repo:
    def __init__(self, clock, limit=2):
        self.clock = clock
        self.limit = limit
        self.reserved = 0.0
        self.completed = []
        self.observations = []
        self.configured = []
        self.fail = False
        self.deny_once_until = None

    def reserve(self, **values):
        if self.fail:
            raise RuntimeError("database unavailable")
        if self.deny_once_until is not None:
            reset = self.deny_once_until
            self.deny_once_until = None
            return QuotaDecision(False, reason="quota_exhausted", reset_at=reset)
        cost = sum(values["costs"].values())
        if self.reserved + cost > self.limit:
            return QuotaDecision(False, reason="quota_exhausted")
        self.reserved += cost
        reservation = QuotaReservation((f"r{len(self.completed) + 1}",), dict(values["costs"]), values["reserved_calls"])
        return QuotaDecision(True, reservation)

    def complete(self, reservation, *, consumed_fraction):
        self.reserved -= sum(reservation.costs.values())
        self.completed.append(consumed_fraction)

    def observe(self, **values): self.observations.append(values)
    def configure_limit(self, **values): self.configured.append(values)


def test_atomic_reservation_denies_capacity_already_reserved_by_another_request():
    clock = Clock()
    runtime = Runtime(clock, [])
    repo = Repo(clock, limit=1)
    manager = QuotaManager(repo, clock=clock)
    definition = adapter(runtime, limit=1)

    first = manager.acquire(
        definition, instance_id=1, capability_key="quote", holder_id="a",
        mode="interactive", deadline=clock() + timedelta(seconds=10), units=1,
    )
    second = manager.acquire(
        definition, instance_id=1, capability_key="quote", holder_id="b",
        mode="interactive", deadline=clock() + timedelta(seconds=10), units=1,
    )

    assert first.allowed and not second.allowed and second.reason == "quota_exhausted"


def test_interactive_request_waits_at_most_once_when_reset_is_inside_budget():
    clock = Clock()
    runtime = Runtime(clock, [])
    repo = Repo(clock, limit=2)
    repo.deny_once_until = clock() + timedelta(milliseconds=500)

    def sleep(seconds): clock.value += timedelta(seconds=seconds)

    manager = QuotaManager(repo, clock=clock, sleep=sleep, interactive_wait_seconds=1)
    decision = manager.acquire(
        adapter(runtime), instance_id=1, capability_key="quote", holder_id="a",
        mode="interactive", deadline=clock() + timedelta(seconds=3), units=1,
    )

    assert decision.allowed and clock() >= repo.clock.value


def test_database_outage_only_consumes_preallocated_unexpired_safety_slice():
    clock = Clock()
    runtime = Runtime(clock, [])
    repo = Repo(clock)
    repo.fail = True
    pool = SafetySlicePool()
    pool.install((SafetySlice("slice", 1, "quote", "requests", 1, clock() + timedelta(seconds=5)),))
    manager = QuotaManager(repo, safety_slices=pool, clock=clock)

    first = manager.acquire(
        adapter(runtime), instance_id=1, capability_key="quote", holder_id="a",
        mode="interactive", deadline=clock() + timedelta(seconds=3), units=1,
    )
    second = manager.acquire(
        adapter(runtime), instance_id=1, capability_key="quote", holder_id="b",
        mode="interactive", deadline=clock() + timedelta(seconds=3), units=1,
    )

    assert first.allowed and first.reason == "degraded_safety_slice"
    assert not second.allowed and second.reason == "quota_control_plane_unavailable"


def test_unknown_limit_without_safety_budget_and_oversized_background_chunk_are_deferred():
    clock = Clock()
    runtime = Runtime(clock, [])
    repo = Repo(clock, limit=1)
    manager = QuotaManager(repo, clock=clock)

    invalid = manager.acquire(
        adapter(runtime, limit=None), instance_id=1, capability_key="quote", holder_id="a",
        mode="background", deadline=clock() + timedelta(seconds=10), units=1,
    )
    plan = manager.plan_chunk(
        adapter(runtime, limit=1), instance_id=1, capability_key="quote", holder_id="chunk",
        units=2, deadline=clock() + timedelta(seconds=10),
    )

    assert not invalid.allowed and invalid.reason == "quota_model_invalid"
    assert not plan.allowed and plan.action == "defer"


def test_top_level_unknown_limit_safety_budget_applies_to_listed_buckets():
    definition = AdapterDefinition(
        "catalog_style", "1", "Catalog style", "tests.adapters.catalog", OBJECT_SCHEMA, OBJECT_SCHEMA,
        frozenset({"quote"}), {"buckets": ["requests"], "unknown_limit_safety_budget": 30}, Runtime(Clock(), []),
    )

    assert quota_bucket_contracts(definition) == {"requests": {"unknown_safety_limit": 30}}


def test_provider_observation_is_bounded_to_declared_bucket_and_merged_by_repository():
    clock = Clock()
    runtime = Runtime(clock, [])
    repo = Repo(clock)
    manager = QuotaManager(repo, clock=clock)
    manager.observe(
        adapter(runtime), instance_id=1, capability_key="quote",
        observations=(
            {"bucket_key": "requests", "observed_limit": 1, "consumed": 1, "source": "header"},
            {"bucket_key": "undeclared", "observed_limit": 999},
        ),
    )

    assert len(repo.observations) == 1
    assert repo.observations[0]["bucket_key"] == "requests"


def test_router_records_quota_skip_and_falls_back_without_calling_exhausted_provider():
    clock = Clock()
    first = Runtime(clock, [{"price": 1, "symbol": "A"}])
    second = Runtime(clock, [{"price": 2, "symbol": "A"}])
    repo = Repo(clock, limit=1)
    manager = QuotaManager(repo, clock=clock)
    contracts = {
        key: {"buckets": {"requests": {"unit": "request", "configured_limit": 1}}}
        for key in ("first", "second")
    }
    repo.reserved = 1
    original_reserve = repo.reserve

    def per_instance_reserve(**values):
        if values["instance_id"] == 1:
            return QuotaDecision(False, reason="quota_exhausted")
        repo.reserved = 0
        return original_reserve(**values)

    repo.reserve = per_instance_reserve
    router, _ = build_router(
        {"first": (first, 0), "second": (second, 0)},
        quota=manager, quota_contracts=contracts,
    )

    result = router.execute("quote", {"symbol": "A"}, calling_feature="quotes.api")

    assert first.calls == 0 and second.calls == 1
    assert result.provenance.attempts[0].skip_reason == "quota_exhausted"


def test_admin_plan_limit_and_safety_slice_use_declared_bucket_and_reserved_capacity():
    clock = Clock()
    runtime = Runtime(clock, [])
    repo = Repo(clock, limit=3)
    manager = QuotaManager(repo, clock=clock)
    definition = adapter(runtime, limit=3)

    manager.configure_plan_limit(
        definition, instance_id=1, capability_key="quote", bucket_key="requests",
        configured_limit=2, metadata={"plan": "free"},
    )
    item = manager.preallocate_safety_slice(
        definition, instance_id=1, capability_key="quote", bucket_key="requests",
        amount=1, holder_id="worker-1", expires_at=clock() + timedelta(seconds=30),
    )

    assert repo.configured[0]["configured_limit"] == 2
    assert item.remaining == 1 and repo.reserved == 1
