"""Immutable routing snapshots, revision pinning, and live Attempt gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .errors import DataRoutingNotReadyError
from .policy import CapabilityDisabledError, RoutingPolicyError


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


class SecretHandle:
    """Opaque process-local reference to a credential record, never secret material."""

    __slots__ = ("_credential_id",)

    def __init__(self, credential_id: int):
        if int(credential_id) <= 0:
            raise ValueError("SecretHandle requires a positive credential ID")
        self._credential_id = int(credential_id)

    def resolve_credential_id(self) -> int:
        return self._credential_id

    def __repr__(self) -> str:
        return "SecretHandle(<opaque>)"

    def __reduce__(self):
        raise TypeError("SecretHandle cannot be serialized")

    def __reduce_ex__(self, protocol):
        del protocol
        raise TypeError("SecretHandle cannot be serialized")


@dataclass(frozen=True, slots=True, order=True)
class SnapshotVersionToken:
    policy_version: int
    policy_updated: int
    instance_updated: int
    capability_updated: int
    credential_updated: int
    health_version: int


@dataclass(frozen=True, slots=True)
class SnapshotEntrySource:
    capability_key: str
    policy_id: int
    policy_version: int
    revision_id: int
    position: int
    instance_id: int
    instance_key: str
    adapter_key: str
    display_name: str
    lifecycle_status: str
    eligibility_status: str
    non_secret_config: Mapping[str, Any]
    eligibility_requirements: Mapping[str, Any]
    stricter_quality_profile: Mapping[str, Any]
    credential_id: int


@dataclass(frozen=True, slots=True)
class SnapshotPolicySource:
    policy_id: int
    capability_key: str
    policy_version: int
    enabled: bool
    disabled_reason: str
    effective_revision_id: int | None
    quality_profile: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RoutingSnapshotEntry:
    position: int
    instance_id: int
    instance_key: str
    adapter_key: str
    display_name: str
    lifecycle_status: str
    eligibility_status: str
    non_secret_config: Mapping[str, Any]
    eligibility_requirements: Mapping[str, Any]
    stricter_quality_profile: Mapping[str, Any]
    secret_handle: SecretHandle


@dataclass(frozen=True, slots=True)
class RoutingSnapshotPolicy:
    policy_id: int
    capability_key: str
    policy_version: int
    revision_id: int | None
    enabled: bool
    disabled_reason: str
    quality_profile: Mapping[str, Any]
    entries: tuple[RoutingSnapshotEntry, ...]


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    version_token: SnapshotVersionToken
    loaded_at: datetime
    policies: Mapping[str, RoutingSnapshotPolicy]

    def pin(self, capability_key: str) -> "PinnedRoutingRevision":
        policy = self.policies.get(capability_key)
        if policy is None or policy.revision_id is None:
            raise DataRoutingNotReadyError(
                "Data Capability has no effective routing revision",
                details={"capability_key": capability_key},
            )
        return PinnedRoutingRevision(
            capability_key=policy.capability_key,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            revision_id=policy.revision_id,
            enabled=policy.enabled,
            disabled_reason=policy.disabled_reason,
            quality_profile=policy.quality_profile,
            entries=policy.entries,
        )


@dataclass(frozen=True, slots=True)
class PinnedRoutingRevision:
    capability_key: str
    policy_id: int
    policy_version: int
    revision_id: int
    enabled: bool
    disabled_reason: str
    quality_profile: Mapping[str, Any]
    entries: tuple[RoutingSnapshotEntry, ...]

    def require_provider_attempts_enabled(self) -> None:
        if not self.enabled:
            raise CapabilityDisabledError(
                "Data Capability is explicitly disabled",
                details={"capability_key": self.capability_key},
            )


@dataclass(frozen=True, slots=True)
class AttemptEligibility:
    allowed: bool
    reason: str = ""


class SnapshotRepository(Protocol):
    def current_version_token(self) -> SnapshotVersionToken: ...

    def load_snapshot_sources(
        self,
    ) -> tuple[SnapshotVersionToken, Sequence[SnapshotPolicySource], Sequence[SnapshotEntrySource]]: ...

    def check_attempt_eligibility(self, instance_id: int, capability_key: str) -> AttemptEligibility: ...


def build_routing_snapshot(
    version_token: SnapshotVersionToken,
    policies: Sequence[SnapshotPolicySource],
    entries: Sequence[SnapshotEntrySource],
) -> RoutingSnapshot:
    grouped: dict[str, list[RoutingSnapshotEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.capability_key, []).append(
            RoutingSnapshotEntry(
                position=entry.position,
                instance_id=entry.instance_id,
                instance_key=entry.instance_key,
                adapter_key=entry.adapter_key,
                display_name=entry.display_name,
                lifecycle_status=entry.lifecycle_status,
                eligibility_status=entry.eligibility_status,
                non_secret_config=_freeze(entry.non_secret_config),
                eligibility_requirements=_freeze(entry.eligibility_requirements),
                stricter_quality_profile=_freeze(entry.stricter_quality_profile),
                secret_handle=SecretHandle(entry.credential_id),
            )
        )
    materialized: dict[str, RoutingSnapshotPolicy] = {}
    for policy in policies:
        ordered = tuple(sorted(grouped.get(policy.capability_key, []), key=lambda item: item.position))
        materialized[policy.capability_key] = RoutingSnapshotPolicy(
            policy_id=policy.policy_id,
            capability_key=policy.capability_key,
            policy_version=policy.policy_version,
            revision_id=policy.effective_revision_id,
            enabled=policy.enabled,
            disabled_reason=policy.disabled_reason,
            quality_profile=_freeze(policy.quality_profile),
            entries=ordered,
        )
    return RoutingSnapshot(version_token, datetime.now(timezone.utc), MappingProxyType(materialized))


class RoutingSnapshotManager:
    """Serializes refreshes while readers atomically observe immutable snapshots."""

    def __init__(
        self,
        repository: SnapshotRepository,
        *,
        stale_window_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.stale_window_seconds = max(1, min(int(stale_window_seconds), 3600))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._refresh_lock = Lock()
        self._current: RoutingSnapshot | None = None
        self._control_plane_degraded_since: datetime | None = None

    def current(self) -> RoutingSnapshot:
        snapshot = self._current
        if snapshot is None:
            raise DataRoutingNotReadyError("RoutingSnapshot has not been loaded")
        if (
            self._control_plane_degraded_since is not None
            and (self.clock() - self._control_plane_degraded_since).total_seconds() > self.stale_window_seconds
        ):
            raise DataRoutingNotReadyError("Last valid RoutingSnapshot exceeded its degraded-control-plane window")
        return snapshot

    def refresh_if_changed(self, *, force: bool = False) -> bool:
        with self._refresh_lock:
            current = self._current
            try:
                observed = self.repository.current_version_token()
                if not force and current is not None and observed == current.version_token:
                    self._control_plane_degraded_since = None
                    return False
                token, policies, entries = self.repository.load_snapshot_sources()
                replacement = build_routing_snapshot(token, policies, entries)
                self._current = replacement
                self._control_plane_degraded_since = None
                return True
            except Exception as exc:
                if current is None:
                    raise DataRoutingNotReadyError("No valid RoutingSnapshot is available") from exc
                if self._control_plane_degraded_since is None:
                    self._control_plane_degraded_since = self.clock()
                if (self.clock() - self._control_plane_degraded_since).total_seconds() > self.stale_window_seconds:
                    raise DataRoutingNotReadyError("RoutingSnapshot degraded window has expired") from exc
                return False

    @property
    def management_changes_allowed(self) -> bool:
        return self._control_plane_degraded_since is None

    def control_plane_health(self) -> Mapping[str, Any]:
        snapshot = self._current
        degraded = self._control_plane_degraded_since is not None
        expired = bool(
            degraded
            and (self.clock() - self._control_plane_degraded_since).total_seconds() > self.stale_window_seconds
        )
        return MappingProxyType({
            "ready": snapshot is not None and not expired,
            "degraded": degraded,
            "management_frozen": degraded,
            "degraded_since": self._control_plane_degraded_since,
            "snapshot_loaded_at": snapshot.loaded_at if snapshot else None,
            "stale_window_seconds": self.stale_window_seconds,
        })

    def readiness(self) -> Mapping[str, Any]:
        health = dict(self.control_plane_health())
        health["reason"] = "" if health["ready"] else "no_valid_routing_snapshot"
        return MappingProxyType(health)

    def pin(self, capability_key: str) -> PinnedRoutingRevision:
        return self.current().pin(capability_key)

    def check_before_attempt(
        self, pinned: PinnedRoutingRevision, entry: RoutingSnapshotEntry
    ) -> AttemptEligibility:
        if entry not in pinned.entries:
            raise RoutingPolicyError("Attempt entry does not belong to the pinned routing revision")
        return self.repository.check_attempt_eligibility(entry.instance_id, pinned.capability_key)


class PostgresRoutingSnapshotRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    @staticmethod
    def _token(cur) -> SnapshotVersionToken:
        cur.execute(
            """SELECT
               COALESCE((SELECT MAX(policy_version) FROM qd_data_routing_policies),0) AS policy_version,
               COALESCE((SELECT MAX((EXTRACT(EPOCH FROM updated_at)*1000000)::BIGINT) FROM qd_data_routing_policies),0) AS policy_updated,
               COALESCE((SELECT MAX((EXTRACT(EPOCH FROM updated_at)*1000000)::BIGINT) FROM qd_provider_instances),0) AS instance_updated,
               COALESCE((SELECT MAX((EXTRACT(EPOCH FROM updated_at)*1000000)::BIGINT) FROM qd_provider_instance_capabilities),0) AS capability_updated,
               COALESCE((SELECT MAX((EXTRACT(EPOCH FROM COALESCE(activated_at,created_at))*1000000)::BIGINT) FROM qd_provider_credentials WHERE status='active'),0) AS credential_updated,
               COALESCE((SELECT SUM(state_version) FROM qd_provider_health_states),0) AS health_version"""
        )
        row = cur.fetchone()
        return SnapshotVersionToken(*(int(row[name]) for name in (
            "policy_version", "policy_updated", "instance_updated", "capability_updated", "credential_updated", "health_version"
        )))

    def current_version_token(self):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                return self._token(cur)
            finally:
                cur.close()

    def load_snapshot_sources(self):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                token = self._token(cur)
                cur.execute(
                    """SELECT p.id AS policy_id,p.capability_key,p.policy_version,p.enabled,p.disabled_reason,
                              p.effective_revision_id,COALESCE(r.quality_profile,'{}'::jsonb) AS quality_profile
                       FROM qd_data_routing_policies p
                       LEFT JOIN qd_data_routing_revisions r ON r.id=p.effective_revision_id
                       ORDER BY p.capability_key"""
                )
                policies = tuple(SnapshotPolicySource(**dict(row)) for row in (cur.fetchall() or []))
                cur.execute(
                    """SELECT p.capability_key,p.id AS policy_id,p.policy_version,e.revision_id,e.position,
                              i.id AS instance_id,i.instance_key,i.adapter_key,i.display_name,i.lifecycle_status,
                              c.eligibility_status,i.non_secret_config,e.eligibility_requirements,
                              e.stricter_quality_profile,credential.id AS credential_id
                       FROM qd_data_routing_policies p
                       JOIN qd_data_routing_entries e ON e.revision_id=p.effective_revision_id
                       JOIN qd_provider_instances i ON i.id=e.instance_id
                       JOIN qd_provider_instance_capabilities c
                         ON c.instance_id=i.id AND c.capability_key=p.capability_key
                       JOIN qd_provider_credentials credential
                         ON credential.instance_id=i.id AND credential.status='active'
                       ORDER BY p.capability_key,e.position"""
                )
                entries = tuple(SnapshotEntrySource(**dict(row)) for row in (cur.fetchall() or []))
                cur.execute(
                    """SELECT COUNT(*) AS count FROM qd_data_routing_policies p
                       JOIN qd_data_routing_entries e ON e.revision_id=p.effective_revision_id"""
                )
                if int(cur.fetchone()["count"]) != len(entries):
                    raise DataRoutingNotReadyError(
                        "effective routing revision contains an Instance without an active credential or Capability record"
                    )
                db.commit()
                return token, policies, entries
            finally:
                cur.close()

    def check_attempt_eligibility(self, instance_id: int, capability_key: str):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT p.enabled,i.lifecycle_status,c.eligibility_status,
                              hi.health_status AS instance_health,hi.circuit_state AS instance_circuit,
                              hc.health_status AS capability_health,hc.circuit_state AS capability_circuit
                       FROM qd_data_routing_policies p
                       JOIN qd_provider_instances i ON i.id=%s
                       LEFT JOIN qd_provider_instance_capabilities c
                         ON c.instance_id=i.id AND c.capability_key=p.capability_key
                       LEFT JOIN qd_provider_health_states hi
                         ON hi.instance_id=i.id AND hi.capability_key IS NULL
                       LEFT JOIN qd_provider_health_states hc
                         ON hc.instance_id=i.id AND hc.capability_key=p.capability_key
                       WHERE p.capability_key=%s""",
                    (instance_id, capability_key),
                )
                row = cur.fetchone()
            finally:
                cur.close()
        if not row:
            return AttemptEligibility(False, "instance_unavailable")
        if not bool(row["enabled"]):
            return AttemptEligibility(False, "capability_disabled")
        if str(row["lifecycle_status"]) != "active":
            return AttemptEligibility(False, "instance_disabled")
        if str(row.get("eligibility_status") or "") != "eligible":
            return AttemptEligibility(False, "capability_ineligible")
        if "quarantined" in {str(row.get("instance_health") or ""), str(row.get("capability_health") or "")}:
            return AttemptEligibility(False, "quarantined")
        circuits = {str(row.get("instance_circuit") or "closed"), str(row.get("capability_circuit") or "closed")}
        if circuits != {"closed"}:
            return AttemptEligibility(False, "circuit_open")
        return AttemptEligibility(True)
