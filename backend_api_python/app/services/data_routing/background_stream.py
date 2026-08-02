"""Pinned Provider source contract for commit-safe background Capability streams."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .errors import DataRoutingError
from .quota import ChunkQuotaPlan, QuotaManager
from .registry import DataRoutingRegistry
from .snapshot import PinnedRoutingRevision, RoutingSnapshotEntry, RoutingSnapshotManager


class CapabilityStreamStopped(DataRoutingError):
    code = "capability_stream_stopped"


@dataclass(frozen=True, slots=True)
class PinnedCapabilityStream:
    stream_id: str
    capability_key: str
    policy_revision_id: int
    instance_id: int
    adapter_key: str
    checkpoint: Mapping[str, Any]
    entry: RoutingSnapshotEntry
    pinned_revision: PinnedRoutingRevision


class BackgroundCapabilityStreamService:
    def __init__(
        self,
        registry: DataRoutingRegistry,
        snapshots: RoutingSnapshotManager,
        *,
        quota: QuotaManager | None = None,
    ):
        self.registry = registry
        self.snapshots = snapshots
        self.quota = quota

    def pin(
        self,
        *,
        stream_id: str,
        capability_key: str,
        checkpoint: Mapping[str, Any],
        instance_id: int | None = None,
    ) -> PinnedCapabilityStream:
        pinned = self.snapshots.pin(capability_key)
        pinned.require_provider_attempts_enabled()
        selected = None
        for entry in pinned.entries:
            if instance_id is not None and entry.instance_id != instance_id:
                continue
            decision = self.snapshots.check_before_attempt(pinned, entry)
            if decision.allowed:
                selected = entry
                break
            if instance_id is not None:
                raise CapabilityStreamStopped(
                    "Requested fixed Provider Instance is not eligible",
                    details={"instance_id": instance_id, "reason": decision.reason},
                )
        if selected is None:
            raise CapabilityStreamStopped("No Provider Instance can be pinned for the Capability stream")
        return PinnedCapabilityStream(
            str(stream_id), capability_key, pinned.revision_id, selected.instance_id,
            selected.adapter_key, dict(checkpoint), selected, pinned,
        )

    def before_next_chunk(
        self,
        stream: PinnedCapabilityStream,
        *,
        units: int,
        deadline: datetime,
    ) -> ChunkQuotaPlan | None:
        decision = self.snapshots.check_before_attempt(stream.pinned_revision, stream.entry)
        if not decision.allowed:
            raise CapabilityStreamStopped(
                "Fixed Provider Instance became unavailable at a safe chunk boundary",
                details={
                    "instance_id": stream.instance_id,
                    "policy_revision_id": stream.policy_revision_id,
                    "reason": decision.reason,
                },
            )
        if self.quota is None:
            return None
        adapter = self.registry.adapters[stream.adapter_key]
        plan = self.quota.plan_chunk(
            adapter, instance_id=stream.instance_id, capability_key=stream.capability_key,
            holder_id=stream.stream_id, units=units, deadline=deadline,
        )
        if not plan.allowed:
            raise CapabilityStreamStopped(
                "Fixed Provider Instance lacks quota for the next commit-safe chunk",
                details={"action": plan.action, "reset_at": plan.reset_at},
            )
        return plan
