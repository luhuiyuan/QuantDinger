from __future__ import annotations

import pickle
from dataclasses import replace

import pytest

from app.services.data_routing.errors import DataRoutingNotReadyError
from app.services.data_routing.models import CapabilityDefinition
from app.services.data_routing.policy import (
    CapabilityDisabledError,
    RoutingPolicyConflictError,
    RoutingPolicyEntry,
    RoutingPolicyError,
    RoutingPolicyRevision,
    RoutingPolicyService,
    RoutingPolicyState,
    build_impact_preview,
)
from app.services.data_routing.registry import DataRoutingRegistry
from app.services.data_routing.snapshot import (
    AttemptEligibility,
    RoutingSnapshotManager,
    SnapshotEntrySource,
    SnapshotPolicySource,
    SnapshotVersionToken,
)


def _gate(value):
    return []


def _registry():
    registry = DataRoutingRegistry(trusted_module_prefixes=("tests.adapters",))
    registry.register_capability(
        CapabilityDefinition(
            key="quote",
            version="1",
            public_name="Quote",
            semantic_family="quote",
            market="US",
            source_module="tests.adapters.quote",
            allowed_constraints=frozenset({"venue", "max_delay"}),
            normalized_contract={"type": "quote"},
            hard_quality_gates=(_gate,),
            strict_profiles={"strict": {"max_delay": 5}},
            cache_key_fields=("subject",),
            freshness_contract={"fresh_seconds": 5},
        )
    )
    registry.freeze()
    return registry


class FakePolicyRepository:
    def __init__(self):
        self.policy_version = 1
        self.policy_id = 1
        self.enabled = True
        self.disabled_reason = ""
        self.effective_id = None
        self.draft_id = None
        self.next_revision_id = 10
        self.revisions = {}
        self.instances = {
            1: ["active", "eligible"],
            2: ["active", "eligible"],
            3: ["disabled", "eligible"],
            4: ["active", "unverified"],
        }
        self.audits = []

    def _validate(self, entries):
        missing = [entry.instance_id for entry in entries if entry.instance_id not in self.instances]
        if missing:
            raise RoutingPolicyError("missing Provider Instance", details={"instance_ids": missing})
        inactive = [entry.instance_id for entry in entries if self.instances[entry.instance_id][0] != "active"]
        if inactive:
            raise RoutingPolicyError("inactive Provider Instance", details={"instance_ids": inactive})
        ineligible = [entry.instance_id for entry in entries if self.instances[entry.instance_id][1] != "eligible"]
        if ineligible:
            raise RoutingPolicyError("unverified Instance Capability", details={"instance_ids": ineligible})

    def _state(self):
        return RoutingPolicyState(
            self.policy_id,
            "quote",
            self.policy_version,
            self.enabled,
            self.disabled_reason,
            self.revisions.get(self.effective_id),
            self.revisions.get(self.draft_id),
        )

    def _check(self, expected):
        if expected != self.policy_version:
            raise RoutingPolicyConflictError("routing policy changed concurrently")

    def get_policy(self, capability_key):
        assert capability_key == "quote"
        return self._state() if self.effective_id is not None or self.draft_id is not None else None

    def list_revisions(self, capability_key):
        assert capability_key == "quote"
        return tuple(sorted(self.revisions.values(), key=lambda item: item.revision_number, reverse=True))

    def save_draft(self, **values):
        expected = values["expected_policy_version"]
        if expected is not None:
            self._check(expected)
        elif self.effective_id is not None or self.draft_id is not None:
            raise RoutingPolicyConflictError("version required")
        entries = tuple(values["entries"])
        self._validate(entries)
        if self.draft_id is None:
            self.draft_id = self.next_revision_id
            self.next_revision_id += 1
            revision_number = len(self.revisions) + 1
        else:
            revision_number = self.revisions[self.draft_id].revision_number
        self.revisions[self.draft_id] = RoutingPolicyRevision(
            self.draft_id,
            self.policy_id,
            "quote",
            revision_number,
            "draft",
            self.effective_id,
            dict(values["quality_profile"]),
            {},
            "",
            entries,
        )
        self.policy_version += 1
        return self._state()

    def publish_draft(self, **values):
        self._check(values["expected_policy_version"])
        if self.draft_id is None:
            raise RoutingPolicyError("no draft")
        draft = self.revisions[self.draft_id]
        if not draft.entries:
            raise RoutingPolicyError("empty")
        self._validate(draft.entries)
        if self.effective_id is not None:
            self.revisions[self.effective_id] = replace(self.revisions[self.effective_id], status="superseded")
        self.revisions[self.draft_id] = replace(
            draft,
            status="published",
            impact_preview=dict(values["impact_preview"]),
            change_reason=values["reason"],
        )
        self.effective_id = self.draft_id
        self.draft_id = None
        self.enabled = True
        self.disabled_reason = ""
        self.policy_version += 1
        self.audits.append("published")
        return self._state()

    def restore_revision(self, **values):
        self._check(values["expected_policy_version"])
        if self.draft_id is not None:
            raise RoutingPolicyConflictError("draft exists")
        source = self.revisions.get(values["source_revision_id"])
        if source is None or source.status not in {"published", "superseded"}:
            raise RoutingPolicyError("history missing")
        self._validate(source.entries)
        self.draft_id = self.next_revision_id
        self.next_revision_id += 1
        self.revisions[self.draft_id] = replace(
            source,
            revision_id=self.draft_id,
            revision_number=len(self.revisions) + 1,
            status="draft",
            based_on_revision_id=source.revision_id,
            impact_preview={},
            change_reason="",
        )
        self.policy_version += 1
        self.audits.append("restored")
        return self._state()

    def disable_capability(self, **values):
        self._check(values["expected_policy_version"])
        self.enabled = False
        self.disabled_reason = values["reason"]
        self.policy_version += 1
        self.audits.append("disabled")
        return self._state()

    def discard_draft(self, **values):
        self._check(values["expected_policy_version"])
        if self.draft_id is None:
            raise RoutingPolicyError("no draft")
        self.revisions[self.draft_id] = replace(
            self.revisions[self.draft_id], status="discarded", change_reason=values["reason"]
        )
        self.draft_id = None
        self.policy_version += 1
        self.audits.append("discarded")
        return self._state()


@pytest.fixture
def policy_setup():
    repository = FakePolicyRepository()
    return RoutingPolicyService(_registry(), repository), repository


def _publish(service, repository, instance_ids):
    state = service.save_draft(
        "quote",
        [{"instance_id": instance_id} for instance_id in instance_ids],
        expected_policy_version=repository.policy_version if repository.get_policy("quote") else None,
        actor_user_id=7,
    )
    return service.publish_draft(
        "quote",
        expected_policy_version=state.policy_version,
        reason="publish validated order",
        actor_user_id=7,
    )


def test_policy_draft_preview_and_publish_preserve_single_ordered_revision(policy_setup):
    service, repository = policy_setup
    state = _publish(service, repository, [1, 2])
    first_revision = state.effective_revision
    draft = service.save_draft(
        "quote",
        [{"instance_id": 2}, {"instance_id": 1}],
        expected_policy_version=state.policy_version,
        actor_user_id=7,
    )

    preview = build_impact_preview(draft)

    assert preview.ordered_instance_ids == (2, 1)
    assert preview.moved_instance_ids == (2, 1)
    published = service.publish_draft(
        "quote", expected_policy_version=draft.policy_version, reason="prefer second", actor_user_id=7
    )
    assert tuple(entry.instance_id for entry in published.effective_revision.entries) == (2, 1)
    assert repository.revisions[first_revision.revision_id].status == "superseded"
    assert first_revision.entries == repository.revisions[first_revision.revision_id].entries
    assert repository.audits == ["published", "published"]


def test_concurrent_publish_allows_only_one_expected_policy_version(policy_setup):
    service, repository = policy_setup
    draft = service.save_draft("quote", [{"instance_id": 1}], actor_user_id=7)
    expected = draft.policy_version

    service.publish_draft("quote", expected_policy_version=expected, reason="first", actor_user_id=7)
    with pytest.raises(RoutingPolicyConflictError):
        service.publish_draft("quote", expected_policy_version=expected, reason="second", actor_user_id=8)


@pytest.mark.parametrize("instance_id", [3, 4, 999])
def test_draft_rejects_inactive_unverified_and_missing_instances(policy_setup, instance_id):
    service, _repository = policy_setup
    with pytest.raises(RoutingPolicyError):
        service.save_draft("quote", [{"instance_id": instance_id}], actor_user_id=7)


def test_empty_draft_requires_explicit_disable_and_disabled_error_is_stable(policy_setup):
    service, repository = policy_setup
    draft = service.save_draft("quote", [], actor_user_id=7)
    with pytest.raises(RoutingPolicyError, match="empty"):
        service.publish_draft("quote", expected_policy_version=draft.policy_version, reason="empty", actor_user_id=7)
    disabled = service.disable_capability(
        "quote", expected_policy_version=draft.policy_version, reason="provider maintenance", actor_user_id=7
    )
    assert disabled.enabled is False and disabled.disabled_reason == "provider maintenance"
    error = CapabilityDisabledError("disabled")
    assert error.code == "capability_disabled"


def test_restore_creates_new_draft_and_revalidates_current_instance_state(policy_setup):
    service, repository = policy_setup
    first = _publish(service, repository, [1])
    historical_id = first.effective_revision.revision_id
    second = service.save_draft(
        "quote", [{"instance_id": 2}], expected_policy_version=first.policy_version, actor_user_id=7
    )
    second = service.publish_draft(
        "quote", expected_policy_version=second.policy_version, reason="switch", actor_user_id=7
    )
    repository.instances[1][1] = "unverified"

    with pytest.raises(RoutingPolicyError, match="unverified"):
        service.restore_revision(
            "quote", historical_id, expected_policy_version=second.policy_version, actor_user_id=7
        )
    repository.instances[1][1] = "eligible"
    restored = service.restore_revision(
        "quote", historical_id, expected_policy_version=second.policy_version, actor_user_id=7
    )
    assert restored.draft_revision.revision_id != historical_id
    assert restored.draft_revision.based_on_revision_id == historical_id
    assert repository.revisions[historical_id].status == "superseded"


def test_history_lists_immutable_revisions_and_draft_discard_is_audited(policy_setup):
    service, repository = policy_setup
    published = _publish(service, repository, [1])
    draft = service.save_draft(
        "quote", [{"instance_id": 2}], expected_policy_version=published.policy_version, actor_user_id=7
    )
    discarded = service.discard_draft(
        "quote",
        expected_policy_version=draft.policy_version,
        reason="replace proposed order",
        actor_user_id=7,
    )
    history = service.list_history("quote")

    assert discarded.draft_revision is None
    assert [revision.status for revision in history] == ["discarded", "published"]
    assert history[-1].entries[0].instance_id == 1
    assert repository.audits[-1] == "discarded"


class FakeSnapshotRepository:
    def __init__(self):
        self.token = SnapshotVersionToken(1, 1, 1, 1, 1, 1)
        self.policy = SnapshotPolicySource(1, "quote", 1, True, "", 10, {})
        self.order = [1, 2]
        self.gates = {1: AttemptEligibility(True), 2: AttemptEligibility(True)}
        self.loads = 0

    def current_version_token(self):
        return self.token

    def load_snapshot_sources(self):
        self.loads += 1
        entries = tuple(
            SnapshotEntrySource(
                "quote", 1, self.policy.policy_version, self.policy.effective_revision_id, position,
                instance_id, f"instance_{instance_id}", "adapter", f"Instance {instance_id}",
                "active", "eligible", {"nested": {"timeout": 5}}, {}, {}, 100 + instance_id,
            )
            for position, instance_id in enumerate(self.order, start=1)
        )
        return self.token, (self.policy,), entries

    def check_attempt_eligibility(self, instance_id, capability_key):
        assert capability_key == "quote"
        return self.gates[instance_id]


def test_snapshot_is_immutable_opaque_and_refreshes_only_when_version_changes():
    repository = FakeSnapshotRepository()
    manager = RoutingSnapshotManager(repository)
    assert manager.refresh_if_changed() is True
    assert manager.refresh_if_changed() is False
    pinned = manager.pin("quote")
    entry = pinned.entries[0]
    with pytest.raises(TypeError):
        entry.non_secret_config["new"] = True
    with pytest.raises(TypeError):
        entry.non_secret_config["nested"]["timeout"] = 10
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(entry.secret_handle)
    assert "101" not in repr(entry.secret_handle)
    assert repository.loads == 1


def test_explicitly_disabled_snapshot_blocks_attempts_after_cache_miss():
    repository = FakeSnapshotRepository()
    repository.policy = SnapshotPolicySource(1, "quote", 2, False, "provider maintenance", 10, {})
    manager = RoutingSnapshotManager(repository)
    manager.refresh_if_changed()

    pinned = manager.pin("quote")

    with pytest.raises(CapabilityDisabledError) as caught:
        pinned.require_provider_attempts_enabled()
    assert caught.value.code == "capability_disabled"
    assert repository.loads == 1


def test_in_flight_request_keeps_pinned_revision_but_rechecks_emergency_state():
    repository = FakeSnapshotRepository()
    manager = RoutingSnapshotManager(repository)
    manager.refresh_if_changed()
    pinned = manager.pin("quote")

    repository.token = SnapshotVersionToken(2, 2, 1, 1, 1, 1)
    repository.policy = SnapshotPolicySource(1, "quote", 2, True, "", 11, {})
    repository.order = [2, 1]
    manager.refresh_if_changed()
    repository.gates[2] = AttemptEligibility(False, "quarantined")

    assert pinned.revision_id == 10
    assert tuple(entry.instance_id for entry in pinned.entries) == (1, 2)
    assert manager.pin("quote").revision_id == 11
    decision = manager.check_before_attempt(pinned, pinned.entries[1])
    assert decision == AttemptEligibility(False, "quarantined")


def test_last_valid_snapshot_has_bounded_degraded_window_and_freezes_management():
    from datetime import datetime, timedelta, timezone

    class Clock:
        value = datetime(2026, 8, 1, tzinfo=timezone.utc)
        def __call__(self): return self.value

    clock = Clock()
    repository = FakeSnapshotRepository()
    manager = RoutingSnapshotManager(repository, stale_window_seconds=30, clock=clock)
    manager.refresh_if_changed(force=True)
    original = manager.current()

    def unavailable(): raise RuntimeError("database unavailable")
    repository.current_version_token = unavailable
    assert manager.refresh_if_changed() is False
    assert manager.current() is original
    assert manager.management_changes_allowed is False
    assert manager.control_plane_health()["degraded"] is True

    clock.value += timedelta(seconds=31)
    with pytest.raises(DataRoutingNotReadyError):
        manager.current()


def test_new_process_without_valid_snapshot_is_not_ready_on_database_failure():
    repository = FakeSnapshotRepository()
    manager = RoutingSnapshotManager(repository)
    repository.current_version_token = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))

    with pytest.raises(DataRoutingNotReadyError):
        manager.refresh_if_changed(force=True)


def test_postgres_publish_revalidates_and_audits_before_commit():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "data_routing"
        / "policy_repository.py"
    ).read_text()
    method = source[source.index("def publish_draft"):source.index("def restore_revision")]
    assert "FOR UPDATE" in method
    assert "self._validate_entries" in method
    assert "FOR SHARE" in source
    assert "self._audit" in method
    assert method.index("self._audit") < method.index("db.commit()")
    assert "policy_version=policy_version+1" in method
