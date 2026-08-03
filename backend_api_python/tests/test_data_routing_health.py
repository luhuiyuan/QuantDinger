from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.services.data_routing.diagnostics import ProviderCapabilityTestService
from app.services.data_routing.health import (
    HealthState,
    PostgresHealthRepository,
    ProviderHealthError,
    ProviderHealthManager,
)
from app.services.data_routing.models import AdapterErrorClassification
from tests.test_data_router import Clock, Resolver, Runtime, build_router


class Repo:
    def __init__(self):
        self.states = {}
        self.evidence = []
        self.probes = {}
        self.fail_cas_once = False

    def get_or_create(self, instance_id, capability_key):
        key = (instance_id, capability_key)
        if key not in self.states:
            self.states[key] = HealthState(len(self.states) + 1, instance_id, capability_key)
        return self.states[key]

    def compare_and_set(self, before, after):
        if self.fail_cas_once:
            self.fail_cas_once = False
            return False
        key = (before.instance_id, before.capability_key)
        if self.states[key].state_version != before.state_version:
            return False
        self.states[key] = after
        return True

    def append_evidence(self, state_id, **values): self.evidence.append((state_id, values))
    def create_probe(self, probe, *, requested_by): self.probes[probe.probe_id] = probe
    def complete_probe(self, probe_id, *, status, result): self.probes[probe_id] = (status, result)


def test_recoverable_state_query_does_not_confuse_jsonb_operator_with_placeholder():
    class Cursor:
        def execute(self, query, params=()):
            converted = query.replace("?", "%s")
            if converted.count("%s") != len(params):
                raise IndexError("tuple index out of range")
            assert "jsonb_exists(failure_window, 'permanent')" in query

        def fetchall(self): return []
        def close(self): pass

    class DB:
        def cursor(self): return Cursor()
        def __enter__(self): return self
        def __exit__(self, *args): return False

    assert PostgresHealthRepository(lambda: DB()).list_recoverable_states(limit=5) == ()


def capability(manager_router):
    return manager_router.registry.capabilities["quote"]


def test_unknown_failure_stays_capability_scoped_and_permanent_auth_opens_instance_scope():
    clock = Clock()
    runtime = Runtime(clock, [])
    router, _ = build_router({"only": (runtime, 0)})
    repo = Repo()
    manager = ProviderHealthManager(repo, clock=clock)

    unknown = manager.record_failure(
        capability(router), instance_id=1,
        classification=AdapterErrorClassification("unknown", False, "unknown", "unknown"),
        routed_request_id="request-1",
    )
    permanent = manager.record_failure(
        capability(router), instance_id=1,
        classification=AdapterErrorClassification("authentication_revoked", False, "permanent", "instance"),
        routed_request_id="request-2",
    )

    assert unknown.capability_key == "quote" and unknown.circuit_state == "closed"
    assert permanent.capability_key is None and permanent.circuit_state == "open"
    assert permanent.health_status == "unhealthy"


def test_transient_failure_requires_minimum_window_samples_and_cas_conflict_retries():
    clock = Clock()
    runtime = Runtime(clock, [])
    router, _ = build_router({"only": (runtime, 0)})
    repo = Repo()
    repo.fail_cas_once = True
    manager = ProviderHealthManager(repo, clock=clock)
    classification = AdapterErrorClassification("timeout", True, "transient", "capability")

    first = manager.record_failure(capability(router), instance_id=1, classification=classification, routed_request_id="1")
    second = manager.record_failure(capability(router), instance_id=1, classification=classification, routed_request_id="2")
    third = manager.record_failure(capability(router), instance_id=1, classification=classification, routed_request_id="3")

    assert first.circuit_state == second.circuit_state == "closed"
    assert third.circuit_state == "open" and third.circuit_until > clock()


def test_recovery_probe_requires_evidence_and_failed_probe_applies_bounded_backoff():
    clock = Clock()
    runtime = Runtime(clock, [])
    router, _ = build_router({"only": (runtime, 0)})
    repo = Repo()
    manager = ProviderHealthManager(repo, clock=clock)
    permanent = AdapterErrorClassification("endpoint_removed", False, "permanent", "capability")
    manager.record_failure(capability(router), instance_id=1, classification=permanent, routed_request_id="1")

    first = manager.start_recovery_probe(
        capability(router), instance_id=1, capability_key="quote", trigger="administrator",
        reason="verify provider recovery",
    )
    half_open = manager.complete_recovery_probe(
        capability(router), probe=first, instance_id=1, capability_key="quote", succeeded=True
    )
    second = manager.start_recovery_probe(
        capability(router), instance_id=1, capability_key="quote", trigger="administrator",
        reason="confirm consecutive recovery evidence",
    )
    healthy = manager.complete_recovery_probe(
        capability(router), probe=second, instance_id=1, capability_key="quote", succeeded=True
    )

    assert half_open.circuit_state == "half_open"
    assert healthy.circuit_state == "closed" and healthy.health_status == "healthy"

    manager.record_failure(capability(router), instance_id=1, classification=permanent, routed_request_id="2")
    failed_probe = manager.start_recovery_probe(
        capability(router), instance_id=1, capability_key="quote", trigger="administrator",
        reason="recheck provider after remediation",
    )
    failed = manager.complete_recovery_probe(
        capability(router), probe=failed_probe, instance_id=1, capability_key="quote", succeeded=False
    )
    assert failed.circuit_state == "open" and failed.circuit_until > clock()


def test_quarantine_blocks_probe_and_administrator_cannot_force_healthy():
    clock = Clock()
    repo = Repo()
    manager = ProviderHealthManager(repo, clock=clock)
    state = manager.quarantine(1, "quote", reason="suspected data corruption")

    assert state.health_status == "quarantined"
    with pytest.raises(ProviderHealthError):
        manager.force_healthy(1, "quote")


def test_administrator_recovery_probe_requires_reason_before_state_change():
    clock = Clock()
    runtime = Runtime(clock, [])
    router, _ = build_router({"only": (runtime, 0)})
    repo = Repo()
    manager = ProviderHealthManager(repo, clock=clock)
    manager.record_failure(
        capability(router), instance_id=1,
        classification=AdapterErrorClassification("endpoint_removed", False, "permanent", "capability"),
        routed_request_id="request-1",
    )
    before = repo.states[(1, "quote")]

    with pytest.raises(ProviderHealthError, match="reason"):
        manager.start_recovery_probe(
            capability(router), instance_id=1, capability_key="quote", trigger="administrator"
        )

    assert repo.states[(1, "quote")] == before
    assert repo.probes == {}


class DiagnosticRepo:
    def __init__(self): self.rows = {}
    def start(self, **values): self.rows[values["diagnostic_id"]] = {"status": "running", **values}
    def complete(self, diagnostic_id, *, status, result): self.rows[diagnostic_id].update(status=status, result=result)


def test_capability_diagnostic_bypasses_routing_and_does_not_mutate_production_circuit():
    clock = Clock()
    runtime = Runtime(clock, [{"price": 12, "symbol": "A"}])
    router, snapshot_repo = build_router({"only": (runtime, 0)})
    snapshot_repo.gates[1] = replace(snapshot_repo.gates[1], allowed=False, reason="circuit_open")
    health_repo = Repo()
    health_repo.states[(1, "quote")] = HealthState(
        1, 1, "quote", "unhealthy", "open", "timeout", clock(), clock() + timedelta(minutes=1)
    )
    diagnostics = DiagnosticRepo()
    service = ProviderCapabilityTestService(router.registry, Resolver(), diagnostics, clock=clock)

    result = service.run(
        router.snapshots.pin("quote").entries[0], capability_key="quote",
        subject={"symbol": "A"}, constraints={}, requested_by=7,
    )

    assert result.succeeded and runtime.calls == 1
    assert health_repo.states[(1, "quote")].circuit_state == "open"
    assert diagnostics.rows[result.diagnostic_id]["status"] == "succeeded"
