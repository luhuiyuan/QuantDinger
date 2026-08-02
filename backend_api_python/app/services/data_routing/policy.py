"""Versioned routing-policy control-plane contracts and service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .errors import DataRoutingError
from .quality import QualityProfileError, resolve_quality_thresholds
from .registry import DataRoutingRegistry


class RoutingPolicyError(DataRoutingError):
    code = "routing_policy_invalid"


class RoutingPolicyConflictError(RoutingPolicyError):
    code = "routing_policy_conflict"


class CapabilityDisabledError(RoutingPolicyError):
    code = "capability_disabled"


@dataclass(frozen=True, slots=True)
class RoutingPolicyEntry:
    instance_id: int
    position: int
    eligibility_requirements: Mapping[str, Any] = field(default_factory=dict)
    stricter_quality_profile: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RoutingPolicyRevision:
    revision_id: int
    policy_id: int
    capability_key: str
    revision_number: int
    status: str
    based_on_revision_id: int | None
    quality_profile: Mapping[str, Any]
    impact_preview: Mapping[str, Any]
    change_reason: str
    entries: tuple[RoutingPolicyEntry, ...]


@dataclass(frozen=True, slots=True)
class RoutingPolicyState:
    policy_id: int
    capability_key: str
    policy_version: int
    enabled: bool
    disabled_reason: str
    effective_revision: RoutingPolicyRevision | None
    draft_revision: RoutingPolicyRevision | None


@dataclass(frozen=True, slots=True)
class RoutingPolicyImpactPreview:
    capability_key: str
    effective_revision_id: int | None
    draft_revision_id: int
    added_instance_ids: tuple[int, ...]
    removed_instance_ids: tuple[int, ...]
    moved_instance_ids: tuple[int, ...]
    ordered_instance_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_key": self.capability_key,
            "effective_revision_id": self.effective_revision_id,
            "draft_revision_id": self.draft_revision_id,
            "added_instance_ids": list(self.added_instance_ids),
            "removed_instance_ids": list(self.removed_instance_ids),
            "moved_instance_ids": list(self.moved_instance_ids),
            "ordered_instance_ids": list(self.ordered_instance_ids),
        }


class RoutingPolicyRepository(Protocol):
    def get_policy(self, capability_key: str) -> RoutingPolicyState | None: ...
    def list_revisions(self, capability_key: str) -> Sequence[RoutingPolicyRevision]: ...

    def save_draft(
        self,
        *,
        capability_key: str,
        entries: Sequence[RoutingPolicyEntry],
        quality_profile: Mapping[str, Any],
        expected_policy_version: int | None,
        actor_user_id: int | None,
    ) -> RoutingPolicyState: ...

    def publish_draft(
        self,
        *,
        capability_key: str,
        expected_policy_version: int,
        reason: str,
        actor_user_id: int | None,
        impact_preview: Mapping[str, Any],
    ) -> RoutingPolicyState: ...

    def restore_revision(
        self,
        *,
        capability_key: str,
        source_revision_id: int,
        expected_policy_version: int,
        actor_user_id: int | None,
        reason: str = "restore_historical_revision",
    ) -> RoutingPolicyState: ...

    def disable_capability(
        self,
        *,
        capability_key: str,
        expected_policy_version: int,
        reason: str,
        actor_user_id: int | None,
    ) -> RoutingPolicyState: ...

    def discard_draft(
        self,
        *,
        capability_key: str,
        expected_policy_version: int,
        reason: str,
        actor_user_id: int | None,
    ) -> RoutingPolicyState: ...


def build_impact_preview(state: RoutingPolicyState) -> RoutingPolicyImpactPreview:
    draft = state.draft_revision
    if draft is None:
        raise RoutingPolicyError("routing policy has no draft revision")
    effective_ids = tuple(entry.instance_id for entry in (state.effective_revision.entries if state.effective_revision else ()))
    draft_ids = tuple(entry.instance_id for entry in draft.entries)
    effective_positions = {instance_id: index for index, instance_id in enumerate(effective_ids)}
    draft_positions = {instance_id: index for index, instance_id in enumerate(draft_ids)}
    return RoutingPolicyImpactPreview(
        capability_key=state.capability_key,
        effective_revision_id=state.effective_revision.revision_id if state.effective_revision else None,
        draft_revision_id=draft.revision_id,
        added_instance_ids=tuple(instance_id for instance_id in draft_ids if instance_id not in effective_positions),
        removed_instance_ids=tuple(instance_id for instance_id in effective_ids if instance_id not in draft_positions),
        moved_instance_ids=tuple(
            instance_id
            for instance_id in draft_ids
            if instance_id in effective_positions and effective_positions[instance_id] != draft_positions[instance_id]
        ),
        ordered_instance_ids=draft_ids,
    )


class RoutingPolicyService:
    def __init__(self, registry: DataRoutingRegistry, repository: RoutingPolicyRepository):
        self.registry = registry
        self.repository = repository

    def _capability(self, capability_key: str):
        capability = self.registry.capabilities.get(str(capability_key or ""))
        if capability is None:
            raise RoutingPolicyError("Data Capability is not registered", details={"capability_key": capability_key})
        return capability

    def _entries(self, capability_key: str, entries: Sequence[Mapping[str, Any] | RoutingPolicyEntry]):
        capability = self._capability(capability_key)
        normalized: list[RoutingPolicyEntry] = []
        seen: set[int] = set()
        for position, raw in enumerate(entries, start=1):
            if isinstance(raw, RoutingPolicyEntry):
                instance_id = int(raw.instance_id)
                requirements = dict(raw.eligibility_requirements)
                profile = dict(raw.stricter_quality_profile)
            else:
                instance_id = int(raw.get("instance_id") or 0)
                requirements = dict(raw.get("eligibility_requirements") or {})
                profile = dict(raw.get("stricter_quality_profile") or {})
            if instance_id <= 0 or instance_id in seen:
                raise RoutingPolicyError("routing policy entries require unique positive Instance IDs")
            unsupported = sorted(set(requirements) - set(capability.allowed_constraints))
            if unsupported:
                raise RoutingPolicyError(
                    "routing policy entry uses undeclared eligibility constraints",
                    details={"instance_id": instance_id, "constraints": unsupported},
                )
            unknown_profiles = sorted(set(profile) - set(capability.strict_profiles))
            if unknown_profiles:
                raise RoutingPolicyError(
                    "routing policy entry uses an undeclared strict quality profile",
                    details={"instance_id": instance_id, "profiles": unknown_profiles},
                )
            try:
                resolve_quality_thresholds(capability, profile)
            except QualityProfileError as exc:
                raise RoutingPolicyError(str(exc), details=exc.details) from exc
            seen.add(instance_id)
            normalized.append(RoutingPolicyEntry(instance_id, position, requirements, profile))
        return tuple(normalized)

    def save_draft(
        self,
        capability_key: str,
        entries: Sequence[Mapping[str, Any] | RoutingPolicyEntry],
        *,
        quality_profile: Mapping[str, Any] | None = None,
        expected_policy_version: int | None = None,
        actor_user_id: int | None = None,
    ) -> RoutingPolicyState:
        capability = self._capability(capability_key)
        selected_profile = dict(quality_profile or {})
        unknown_profiles = sorted(set(selected_profile) - set(capability.strict_profiles))
        if unknown_profiles:
            raise RoutingPolicyError("routing policy uses an undeclared quality profile", details={"profiles": unknown_profiles})
        try:
            resolve_quality_thresholds(capability, selected_profile)
        except QualityProfileError as exc:
            raise RoutingPolicyError(str(exc), details=exc.details) from exc
        return self.repository.save_draft(
            capability_key=capability_key,
            entries=self._entries(capability_key, entries),
            quality_profile=selected_profile,
            expected_policy_version=expected_policy_version,
            actor_user_id=actor_user_id,
        )

    def preview_draft(self, capability_key: str) -> RoutingPolicyImpactPreview:
        self._capability(capability_key)
        state = self.repository.get_policy(capability_key)
        if state is None:
            raise RoutingPolicyError("routing policy does not exist")
        return build_impact_preview(state)

    def list_history(self, capability_key: str) -> tuple[RoutingPolicyRevision, ...]:
        self._capability(capability_key)
        return tuple(self.repository.list_revisions(capability_key))

    def publish_draft(
        self,
        capability_key: str,
        *,
        expected_policy_version: int,
        reason: str,
        actor_user_id: int | None,
    ) -> RoutingPolicyState:
        if not str(reason or "").strip():
            raise RoutingPolicyError("routing policy publish reason is required")
        self._capability(capability_key)
        state = self.repository.get_policy(capability_key)
        if state is None:
            raise RoutingPolicyError("routing policy does not exist")
        if state.policy_version != int(expected_policy_version):
            raise RoutingPolicyConflictError(
                "routing policy changed concurrently",
                details={
                    "expected_policy_version": int(expected_policy_version),
                    "current_policy_version": state.policy_version,
                },
            )
        preview = build_impact_preview(state)
        if not preview.ordered_instance_ids:
            raise RoutingPolicyError("enabled routing policy cannot publish an empty Instance list")
        return self.repository.publish_draft(
            capability_key=capability_key,
            expected_policy_version=expected_policy_version,
            reason=str(reason).strip(),
            actor_user_id=actor_user_id,
            impact_preview=preview.as_dict(),
        )

    def restore_revision(
        self,
        capability_key: str,
        source_revision_id: int,
        *,
        expected_policy_version: int,
        actor_user_id: int | None,
        reason: str = "restore_historical_revision",
    ) -> RoutingPolicyState:
        self._capability(capability_key)
        if not str(reason or "").strip():
            raise RoutingPolicyError("routing policy restore reason is required")
        return self.repository.restore_revision(
            capability_key=capability_key,
            source_revision_id=int(source_revision_id),
            expected_policy_version=expected_policy_version,
            actor_user_id=actor_user_id,
            reason=str(reason).strip(),
        )

    def disable_capability(
        self,
        capability_key: str,
        *,
        expected_policy_version: int,
        reason: str,
        actor_user_id: int | None,
    ) -> RoutingPolicyState:
        self._capability(capability_key)
        if not str(reason or "").strip():
            raise RoutingPolicyError("Capability disable reason is required")
        return self.repository.disable_capability(
            capability_key=capability_key,
            expected_policy_version=expected_policy_version,
            reason=str(reason).strip(),
            actor_user_id=actor_user_id,
        )

    def discard_draft(
        self,
        capability_key: str,
        *,
        expected_policy_version: int,
        reason: str,
        actor_user_id: int | None,
    ) -> RoutingPolicyState:
        self._capability(capability_key)
        if not str(reason or "").strip():
            raise RoutingPolicyError("routing policy draft discard reason is required")
        return self.repository.discard_draft(
            capability_key=capability_key,
            expected_policy_version=expected_policy_version,
            reason=str(reason).strip(),
            actor_user_id=actor_user_id,
        )
