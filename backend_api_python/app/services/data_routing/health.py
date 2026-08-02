"""Provider health evidence, scoped circuits, quarantine, and recovery probes."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from .errors import DataRoutingError
from .models import AdapterErrorClassification, CapabilityDefinition


class ProviderHealthError(DataRoutingError):
    code = "provider_health_error"


@dataclass(frozen=True, slots=True)
class HealthState:
    state_id: int
    instance_id: int
    capability_key: str | None
    health_status: str = "unknown"
    circuit_state: str = "closed"
    circuit_reason: str = ""
    circuit_opened_at: datetime | None = None
    circuit_until: datetime | None = None
    quarantine_reason: str = ""
    quarantined_at: datetime | None = None
    failure_window: Mapping[str, Any] = None
    state_version: int = 1

    def __post_init__(self):
        if self.failure_window is None:
            object.__setattr__(self, "failure_window", {})


@dataclass(frozen=True, slots=True)
class RecoveryProbe:
    probe_id: str
    state_id: int
    trigger: str
    status: str
    not_before: datetime
    attempt_number: int


class HealthRepository(Protocol):
    def get_or_create(self, instance_id: int, capability_key: str | None) -> HealthState: ...
    def compare_and_set(self, before: HealthState, after: HealthState) -> bool: ...
    def append_evidence(
        self, state_id: int, *, routed_request_id: str | None, evidence_kind: str,
        permanence: str, summary: str, metadata: Mapping[str, Any],
    ) -> None: ...
    def create_probe(self, probe: RecoveryProbe, *, requested_by: int | None) -> None: ...
    def complete_probe(self, probe_id: str, *, status: str, result: Mapping[str, Any]) -> None: ...
    def list_recoverable_states(self, *, limit: int) -> tuple[HealthState, ...]: ...
    def list_due_probes(self, *, limit: int) -> tuple[tuple[RecoveryProbe, HealthState], ...]: ...


def _policy(capability: CapabilityDefinition) -> dict[str, Any]:
    raw = dict(capability.health_contract or {})
    return {
        "window_seconds": max(10, min(int(raw.get("window_seconds") or 300), 3600)),
        "transient_failure_threshold": max(2, min(int(raw.get("transient_failure_threshold") or 3), 20)),
        "cooldown_seconds": max(10, min(int(raw.get("cooldown_seconds") or 60), 3600)),
        "max_cooldown_seconds": max(60, min(int(raw.get("max_cooldown_seconds") or 3600), 86400)),
        "probe_success_threshold": max(1, min(int(raw.get("probe_success_threshold") or 2), 10)),
    }


class ProviderHealthManager:
    def __init__(
        self,
        repository: HealthRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def scope_capability(capability_key: str, classification: AdapterErrorClassification) -> str | None:
        return None if classification.scope_hint == "instance" else capability_key

    def _update(self, instance_id: int, capability_key: str | None, transform) -> HealthState:
        for _ in range(3):
            before = self.repository.get_or_create(instance_id, capability_key)
            after = replace(transform(before), state_version=before.state_version + 1)
            if self.repository.compare_and_set(before, after):
                return after
        raise ProviderHealthError("Provider health state changed concurrently")

    def record_failure(
        self,
        capability: CapabilityDefinition,
        *,
        instance_id: int,
        classification: AdapterErrorClassification,
        routed_request_id: str | None,
        summary: str = "",
    ) -> HealthState:
        now = self.clock()
        scope = self.scope_capability(capability.key, classification)
        policy = _policy(capability)

        def transform(state: HealthState) -> HealthState:
            window = dict(state.failure_window)
            timestamps = [
                datetime.fromisoformat(item) for item in window.get("transient_failures", ())
                if datetime.fromisoformat(item) >= now - timedelta(seconds=policy["window_seconds"])
            ]
            permanence = classification.permanence if classification.permanence in {"permanent", "transient"} else "unknown"
            if permanence == "permanent":
                return replace(
                    state, health_status="unhealthy", circuit_state="open",
                    circuit_reason=classification.category, circuit_opened_at=now,
                    circuit_until=None, failure_window={**window, "permanent": classification.category},
                )
            timestamps.append(now.isoformat())
            window["transient_failures"] = [item.isoformat() if isinstance(item, datetime) else item for item in timestamps]
            if len(timestamps) < policy["transient_failure_threshold"]:
                return replace(state, health_status="degraded", failure_window=window)
            open_count = int(window.get("open_count") or 0) + 1
            cooldown = min(policy["max_cooldown_seconds"], policy["cooldown_seconds"] * (2 ** (open_count - 1)))
            window.update({"open_count": open_count, "probe_successes": 0})
            return replace(
                state, health_status="unhealthy", circuit_state="open",
                circuit_reason=classification.category or "transient_failure_threshold",
                circuit_opened_at=now, circuit_until=now + timedelta(seconds=cooldown),
                failure_window=window,
            )

        state = self._update(instance_id, scope, transform)
        self.repository.append_evidence(
            state.state_id, routed_request_id=routed_request_id,
            evidence_kind=classification.category or "unclassified",
            permanence=classification.permanence if classification.permanence in {"permanent", "transient"} else "unknown",
            summary=summary[:2000], metadata={"scope": "instance" if scope is None else "capability"},
        )
        return state

    def record_success(
        self, capability_key: str, *, instance_id: int, routed_request_id: str | None
    ) -> HealthState:
        def transform(state: HealthState) -> HealthState:
            if state.circuit_state != "closed" or state.health_status == "quarantined":
                return state
            return replace(state, health_status="healthy", failure_window={})

        state = self._update(instance_id, capability_key, transform)
        self.repository.append_evidence(
            state.state_id, routed_request_id=routed_request_id, evidence_kind="request_success",
            permanence="success", summary="", metadata={},
        )
        return state

    def quarantine(
        self, instance_id: int, capability_key: str | None, *, reason: str
    ) -> HealthState:
        if not str(reason or "").strip():
            raise ProviderHealthError("Quarantine reason is required")
        now = self.clock()
        return self._update(
            instance_id, capability_key,
            lambda state: replace(
                state, health_status="quarantined", circuit_state="open",
                circuit_reason="administrator_quarantine", circuit_opened_at=now,
                circuit_until=None, quarantine_reason=str(reason).strip(), quarantined_at=now,
            ),
        )

    def extend_circuit(
        self, instance_id: int, capability_key: str | None, *, until: datetime, reason: str
    ) -> HealthState:
        if until <= self.clock() or not str(reason or "").strip():
            raise ProviderHealthError("Circuit extension requires a future time and reason")

        def transform(state: HealthState) -> HealthState:
            if state.circuit_state == "closed":
                raise ProviderHealthError("A closed circuit cannot be administratively opened as healthy evidence")
            return replace(state, circuit_until=max(filter(None, (state.circuit_until, until))), circuit_reason=str(reason).strip())

        return self._update(instance_id, capability_key, transform)

    def start_recovery_probe(
        self,
        capability: CapabilityDefinition,
        *,
        instance_id: int,
        capability_key: str | None,
        trigger: str,
        requested_by: int | None = None,
        reason: str = "",
    ) -> RecoveryProbe:
        if trigger not in {"scheduled", "administrator"}:
            raise ProviderHealthError("Recovery probe trigger is invalid")
        now = self.clock()
        def transform(state: HealthState) -> HealthState:
            if state.health_status == "quarantined" or state.circuit_state not in {"open", "half_open"}:
                raise ProviderHealthError("Recovery probe requires a non-quarantined open circuit")
            if trigger == "scheduled" and dict(state.failure_window).get("permanent"):
                raise ProviderHealthError("Permanent failures require configuration evidence before recovery probing")
            if trigger == "scheduled" and state.circuit_until and state.circuit_until > now:
                raise ProviderHealthError("Recovery probe cooldown has not elapsed")
            return replace(state, circuit_state="probe_pending")

        if trigger == "administrator" and not str(reason or "").strip():
            raise ProviderHealthError("Administrator recovery probe reason is required")

        atomic_create = getattr(self.repository, "compare_and_set_and_create_probe", None)
        if atomic_create is not None:
            for _ in range(3):
                before = self.repository.get_or_create(instance_id, capability_key)
                updated = replace(transform(before), state_version=before.state_version + 1)
                window = dict(updated.failure_window)
                probe = RecoveryProbe(
                    uuid.uuid4().hex, updated.state_id, trigger, "queued", now,
                    int(window.get("probe_attempts") or 0) + 1,
                )
                if atomic_create(
                    before, updated, probe, requested_by=requested_by, reason=str(reason or "").strip()
                ):
                    return probe
            raise ProviderHealthError("Provider health state changed concurrently")

        updated = self._update(instance_id, capability_key, transform)
        window = dict(updated.failure_window)
        probe = RecoveryProbe(
            uuid.uuid4().hex, updated.state_id, trigger, "queued", now,
            int(window.get("probe_attempts") or 0) + 1,
        )
        self.repository.create_probe(probe, requested_by=requested_by)
        return probe

    def complete_recovery_probe(
        self,
        capability: CapabilityDefinition,
        *,
        probe: RecoveryProbe,
        instance_id: int,
        capability_key: str | None,
        succeeded: bool,
    ) -> HealthState:
        now = self.clock()
        policy = _policy(capability)

        def transform(state: HealthState) -> HealthState:
            if state.circuit_state not in {"probe_pending", "half_open"}:
                raise ProviderHealthError("Recovery probe is not pending")
            window = dict(state.failure_window)
            window["probe_attempts"] = int(window.get("probe_attempts") or 0) + 1
            if succeeded:
                successes = int(window.get("probe_successes") or 0) + 1
                window["probe_successes"] = successes
                if successes >= policy["probe_success_threshold"]:
                    return replace(
                        state, health_status="healthy", circuit_state="closed", circuit_reason="",
                        circuit_opened_at=None, circuit_until=None, failure_window={},
                    )
                return replace(state, circuit_state="half_open", failure_window=window)
            open_count = int(window.get("open_count") or 1) + 1
            cooldown = min(policy["max_cooldown_seconds"], policy["cooldown_seconds"] * (2 ** (open_count - 1)))
            window.update({"open_count": open_count, "probe_successes": 0})
            return replace(
                state, health_status="unhealthy", circuit_state="open",
                circuit_reason="recovery_probe_failed", circuit_opened_at=now,
                circuit_until=now + timedelta(seconds=cooldown), failure_window=window,
            )

        state = self._update(instance_id, capability_key, transform)
        self.repository.complete_probe(
            probe.probe_id, status="succeeded" if succeeded else "failed",
            result={"health_status": state.health_status, "circuit_state": state.circuit_state},
        )
        self.repository.append_evidence(
            state.state_id, routed_request_id=None, evidence_kind="recovery_probe",
            permanence="success" if succeeded else "transient", summary="", metadata={"probe_id": probe.probe_id},
        )
        return state

    @staticmethod
    def force_healthy(*args, **kwargs):
        del args, kwargs
        raise ProviderHealthError("Administrators cannot force a Provider healthy or close its circuit")


class PostgresHealthRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    @staticmethod
    def _state(row) -> HealthState:
        value = dict(row)
        return HealthState(
            state_id=int(value["id"]), instance_id=int(value["instance_id"]),
            capability_key=value.get("capability_key"), health_status=value["health_status"],
            circuit_state=value["circuit_state"], circuit_reason=value["circuit_reason"],
            circuit_opened_at=value.get("circuit_opened_at"), circuit_until=value.get("circuit_until"),
            quarantine_reason=value["quarantine_reason"], quarantined_at=value.get("quarantined_at"),
            failure_window=dict(value.get("failure_window") or {}), state_version=int(value["state_version"]),
        )

    def get_or_create(self, instance_id: int, capability_key: str | None) -> HealthState:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_provider_health_states(instance_id,capability_key)
                       VALUES (%s,%s) ON CONFLICT (instance_id,(COALESCE(capability_key,''))) DO NOTHING""",
                    (instance_id, capability_key),
                )
                cur.execute(
                    """SELECT * FROM qd_provider_health_states
                       WHERE instance_id=%s AND capability_key IS NOT DISTINCT FROM %s""",
                    (instance_id, capability_key),
                )
                row = cur.fetchone()
                db.commit()
                return self._state(row)
            finally:
                cur.close()

    def compare_and_set(self, before: HealthState, after: HealthState) -> bool:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_health_states SET health_status=%s,circuit_state=%s,
                       circuit_reason=%s,circuit_opened_at=%s,circuit_until=%s,quarantine_reason=%s,
                       quarantined_at=%s,failure_window=%s::jsonb,state_version=%s,last_evidence_at=NOW(),updated_at=NOW()
                       WHERE id=%s AND state_version=%s""",
                    (
                        after.health_status, after.circuit_state, after.circuit_reason,
                        after.circuit_opened_at, after.circuit_until, after.quarantine_reason,
                        after.quarantined_at, json.dumps(dict(after.failure_window), sort_keys=True),
                        after.state_version, before.state_id, before.state_version,
                    ),
                )
                changed = cur.rowcount == 1
                db.commit()
                return changed
            finally:
                cur.close()

    def append_evidence(self, state_id: int, **values) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_provider_health_evidence
                       (health_state_id,routed_request_id,evidence_kind,permanence,sanitized_summary,metadata)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                    (
                        state_id, values["routed_request_id"], values["evidence_kind"],
                        values["permanence"], values["summary"],
                        json.dumps(dict(values["metadata"]), sort_keys=True),
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def create_probe(self, probe: RecoveryProbe, *, requested_by: int | None) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_provider_recovery_probes
                       (probe_id,health_state_id,requested_by,trigger,status,not_before,attempt_number)
                       VALUES (%s,%s,%s,%s,'queued',%s,%s)""",
                    (probe.probe_id, probe.state_id, requested_by, probe.trigger, probe.not_before, probe.attempt_number),
                )
                db.commit()
            finally:
                cur.close()

    def compare_and_set_and_create_probe(
        self,
        before: HealthState,
        after: HealthState,
        probe: RecoveryProbe,
        *,
        requested_by: int | None,
        reason: str,
    ) -> bool:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_health_states SET health_status=%s,circuit_state=%s,
                       circuit_reason=%s,circuit_opened_at=%s,circuit_until=%s,quarantine_reason=%s,
                       quarantined_at=%s,failure_window=%s::jsonb,state_version=%s,last_evidence_at=NOW(),updated_at=NOW()
                       WHERE id=%s AND state_version=%s""",
                    (
                        after.health_status, after.circuit_state, after.circuit_reason,
                        after.circuit_opened_at, after.circuit_until, after.quarantine_reason,
                        after.quarantined_at, json.dumps(dict(after.failure_window), sort_keys=True),
                        after.state_version, before.state_id, before.state_version,
                    ),
                )
                if cur.rowcount != 1:
                    db.rollback()
                    return False
                cur.execute(
                    """INSERT INTO qd_provider_recovery_probes
                       (probe_id,health_state_id,requested_by,trigger,status,not_before,attempt_number)
                       VALUES (%s,%s,%s,%s,'queued',%s,%s)""",
                    (probe.probe_id, probe.state_id, requested_by, probe.trigger, probe.not_before, probe.attempt_number),
                )
                if requested_by is not None:
                    cur.execute(
                        """INSERT INTO qd_data_source_audit
                           (actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
                           VALUES (%s,'provider_recovery_probe_requested','provider_health',%s,%s,%s::jsonb,%s::jsonb,%s,'succeeded')""",
                        (
                            requested_by, str(before.state_id), reason,
                            json.dumps({"circuit_state": before.circuit_state, "state_version": before.state_version}),
                            json.dumps({"circuit_state": after.circuit_state, "probe_id": probe.probe_id}),
                            uuid.uuid4().hex,
                        ),
                    )
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def complete_probe(self, probe_id: str, *, status: str, result: Mapping[str, Any]) -> None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_recovery_probes SET status=%s,sanitized_result=%s::jsonb,completed_at=NOW()
                       WHERE probe_id=%s AND status IN ('queued','running')""",
                    (status, json.dumps(dict(result), sort_keys=True), probe_id),
                )
                db.commit()
            finally:
                cur.close()

    def list_recoverable_states(self, *, limit: int) -> tuple[HealthState, ...]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT * FROM qd_provider_health_states
                       WHERE circuit_state='open' AND health_status<>'quarantined'
                         AND circuit_until IS NOT NULL AND circuit_until<=NOW()
                         AND NOT (failure_window ? 'permanent')
                       ORDER BY circuit_until,id LIMIT %s""",
                    (max(1, min(int(limit), 50)),),
                )
                return tuple(self._state(row) for row in (cur.fetchall() or []))
            finally:
                cur.close()

    def list_due_probes(self, *, limit: int) -> tuple[tuple[RecoveryProbe, HealthState], ...]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT p.*,h.*,
                              p.probe_id AS selected_probe_id,p.status AS probe_status,
                              p.trigger AS probe_trigger,p.attempt_number AS probe_attempt_number,
                              p.not_before AS probe_not_before
                       FROM qd_provider_recovery_probes p
                       JOIN qd_provider_health_states h ON h.id=p.health_state_id
                       WHERE p.status='queued' AND p.not_before<=NOW()
                       ORDER BY p.not_before,p.probe_id LIMIT %s""",
                    (max(1, min(int(limit), 20)),),
                )
                values = []
                for row in cur.fetchall() or []:
                    item = dict(row)
                    probe = RecoveryProbe(
                        item["selected_probe_id"], int(item["id"]), item["probe_trigger"],
                        item["probe_status"], item["probe_not_before"], int(item["probe_attempt_number"]),
                    )
                    values.append((probe, self._state(item)))
                return tuple(values)
            finally:
                cur.close()


def run_provider_health_maintenance(*, limit: int = 5) -> dict[str, int]:
    """Run a bounded serial recovery cycle for the Task Scheduler."""

    from .bootstrap import load_default_data_routing_registry
    from .diagnostics import PostgresDiagnosticRepository, ProviderCapabilityTestService
    from .quota import PostgresQuotaRepository, QuotaManager
    from .router_repository import PostgresRouterSecretResolver
    from .snapshot import PostgresRoutingSnapshotRepository, RoutingSnapshotManager

    bounded = max(1, min(int(limit), 20))
    registry = load_default_data_routing_registry()
    repository = PostgresHealthRepository()
    manager = ProviderHealthManager(repository)
    scheduled = 0
    for state in repository.list_recoverable_states(limit=bounded):
        capability = registry.capabilities.get(str(state.capability_key or ""))
        if capability is None:
            continue
        try:
            manager.start_recovery_probe(
                capability, instance_id=state.instance_id, capability_key=state.capability_key,
                trigger="scheduled",
            )
            scheduled += 1
        except ProviderHealthError:
            continue

    snapshots = RoutingSnapshotManager(PostgresRoutingSnapshotRepository())
    snapshots.refresh_if_changed(force=True)
    diagnostics = ProviderCapabilityTestService(
        registry, PostgresRouterSecretResolver(), PostgresDiagnosticRepository(),
        quota=QuotaManager(PostgresQuotaRepository()),
    )
    succeeded = failed = 0
    for probe, state in repository.list_due_probes(limit=bounded):
        capability = registry.capabilities.get(str(state.capability_key or ""))
        if capability is None:
            failed += 1
            continue
        try:
            pinned = snapshots.pin(capability.key)
            entry = next(item for item in pinned.entries if item.instance_id == state.instance_id)
            result = diagnostics.run(
                entry, capability_key=capability.key, subject={}, constraints={}, requested_by=None,
            )
            manager.complete_recovery_probe(
                capability, probe=probe, instance_id=state.instance_id,
                capability_key=state.capability_key, succeeded=result.succeeded,
            )
            succeeded += int(result.succeeded)
            failed += int(not result.succeeded)
        except Exception:
            try:
                manager.complete_recovery_probe(
                    capability, probe=probe, instance_id=state.instance_id,
                    capability_key=state.capability_key, succeeded=False,
                )
            except Exception:
                pass
            failed += 1
    return {"scheduled": scheduled, "succeeded": succeeded, "failed": failed}
