from __future__ import annotations

from types import SimpleNamespace

from app.services.data_routing.default_bootstrap import (
    BOOTSTRAP_DISABLED_REASON,
    BootstrapProviderInstance,
    DefaultRoutingBootstrapService,
)


class BootstrapRepository:
    def __init__(self):
        self.instances = []
        self.eligible = {}

    def list_non_retired_instances(self):
        return tuple(self.instances)

    def list_eligible_instances(self):
        return {key: dict(value) for key, value in self.eligible.items()}


class InstanceService:
    def __init__(self, repository):
        self.repository = repository
        self.created = []

    def create_draft(self, **values):
        record = BootstrapProviderInstance(
            len(self.repository.instances) + 1,
            values["instance_key"],
            values["adapter_key"],
            "draft",
        )
        self.repository.instances.append(record)
        self.created.append(values)
        return SimpleNamespace(id=record.instance_id)


class CredentialService:
    def __init__(self, repository, registry):
        self.repository = repository
        self.registry = registry
        self.configured = set()
        self.submissions = []

    def get_status(self, instance_id):
        return SimpleNamespace(configured=instance_id in self.configured)

    def ensure_declared_capabilities(self, instance_id):
        # Capability catalog materialization is exercised by the real repository;
        # this bootstrap fake only needs to expose the service contract.
        return None

    def submit_and_validate(self, instance_id, credentials, **values):
        self.submissions.append((instance_id, credentials, values))
        self.configured.add(instance_id)
        instance = next(item for item in self.repository.instances if item.instance_id == instance_id)
        self.repository.instances.remove(instance)
        self.repository.instances.append(
            BootstrapProviderInstance(instance.instance_id, instance.instance_key, instance.adapter_key, "active")
        )
        adapter = self.registry.adapters[instance.adapter_key]
        for capability in adapter.capabilities:
            self.repository.eligible.setdefault(capability, {})[instance.adapter_key] = instance_id
        return SimpleNamespace(configured=True)


class PolicyRepository:
    def __init__(self):
        self.states = {}

    def get_policy(self, capability_key):
        return self.states.get(capability_key)


class PolicyService:
    def __init__(self, repository):
        self.repository = repository
        self.saved = []
        self.published = []
        self.disabled = []

    def save_draft(self, capability_key, entries, **values):
        self.saved.append((capability_key, tuple(entries), values))
        state = SimpleNamespace(
            policy_version=2, enabled=True, disabled_reason="",
            effective_revision=None, draft_revision=object(),
        )
        self.repository.states[capability_key] = state
        return state

    def publish_draft(self, capability_key, **values):
        self.published.append((capability_key, values))
        state = SimpleNamespace(
            policy_version=3, enabled=True, disabled_reason="",
            effective_revision=object(), draft_revision=None,
        )
        self.repository.states[capability_key] = state
        return state

    def disable_capability(self, capability_key, **values):
        self.disabled.append((capability_key, values))
        state = SimpleNamespace(
            policy_version=2, enabled=False, disabled_reason=values["reason"],
            effective_revision=None, draft_revision=None,
        )
        self.repository.states[capability_key] = state
        return state


def registry():
    return SimpleNamespace(
        adapters={
            "public_a": SimpleNamespace(
                key="public_a", version="1", public_name="Public A",
                credential_schema={"required": []}, capabilities=frozenset({"quote"}),
            ),
            "secret_b": SimpleNamespace(
                key="secret_b", version="1", public_name="Secret B",
                credential_schema={"required": ["api_key"]}, capabilities=frozenset({"macro"}),
            ),
        },
        capabilities={"quote": object(), "macro": object()},
    )


def service_fixture():
    selected_registry = registry()
    bootstrap_repository = BootstrapRepository()
    instance_service = InstanceService(bootstrap_repository)
    credential_service = CredentialService(bootstrap_repository, selected_registry)
    policy_repository = PolicyRepository()
    policy_service = PolicyService(policy_repository)
    service = DefaultRoutingBootstrapService(
        selected_registry,
        bootstrap_repository,
        instance_service,
        credential_service,
        policy_repository,
        policy_service,
        preferences={"quote": ("public_a",)},
    )
    return service, bootstrap_repository, instance_service, credential_service, policy_repository, policy_service


def test_first_run_creates_public_provider_publishes_verified_route_and_disables_rest():
    service, _, instances, credentials, _, policies = service_fixture()

    result = service.bootstrap(actor_user_id=1)

    assert result.created_adapters == ("public_a",)
    assert result.validated_adapters == ("public_a",)
    assert result.published_capabilities == ("quote",)
    assert result.disabled_capabilities == ("macro",)
    assert len(instances.created) == 1
    assert credentials.submissions[0][1] == {}
    assert policies.saved[0][1] == ({"instance_id": 1},)
    assert policies.disabled[0][1]["reason"] == BOOTSTRAP_DISABLED_REASON


def test_second_run_is_idempotent_and_does_not_repeat_validation_or_policy_changes():
    service, repository, instances, credentials, _, policies = service_fixture()
    service.bootstrap(actor_user_id=1)
    first_counts = (len(instances.created), len(credentials.submissions), len(policies.saved), len(policies.disabled))

    result = service.bootstrap(actor_user_id=1)

    assert (len(instances.created), len(credentials.submissions), len(policies.saved), len(policies.disabled)) == first_counts
    assert len(repository.instances) == 1
    assert set(result.preserved_capabilities) == {"quote", "macro"}


def test_existing_admin_policy_and_manual_disable_are_preserved():
    service, repository, _, credentials, policy_repository, policies = service_fixture()
    repository.instances.append(BootstrapProviderInstance(7, "custom_public", "public_a", "active"))
    credentials.configured.add(7)
    repository.eligible = {"quote": {"public_a": 7}}
    policy_repository.states["quote"] = SimpleNamespace(
        policy_version=8, enabled=True, disabled_reason="",
        effective_revision=object(), draft_revision=None,
    )
    policy_repository.states["macro"] = SimpleNamespace(
        policy_version=4, enabled=False, disabled_reason="operator maintenance",
        effective_revision=None, draft_revision=None,
    )

    result = service.bootstrap(actor_user_id=1, retry_failed=True)

    assert set(result.preserved_capabilities) == {"quote", "macro"}
    assert policies.saved == [] and policies.published == [] and policies.disabled == []
    assert credentials.submissions == []


def test_dry_run_reports_plan_without_mutating_state():
    service, repository, instances, credentials, _, policies = service_fixture()

    result = service.bootstrap(actor_user_id=1, dry_run=True)

    assert result.planned_adapters == ("public_a",)
    assert repository.instances == []
    assert instances.created == [] and credentials.submissions == []
    assert policies.saved == [] and policies.disabled == []
