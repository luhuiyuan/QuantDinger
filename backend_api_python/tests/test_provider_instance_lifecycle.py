from dataclasses import replace

import pytest

from app.services.data_routing.provider_instances import ProviderInstanceRecord, ProviderInstanceService, ProviderInstanceStateError


class Repo:
    def __init__(self, status="draft"):
        self.record = ProviderInstanceRecord(1,"example_primary","example","Example",status,"1",{},1)
        self.eligible = 0; self.route = False; self.history = False; self.deleted = False
    def create_draft(self, **values): return self.record
    def get(self, instance_id): return self.record if instance_id == 1 else None
    def transition(self, **values):
        self.record = replace(self.record, lifecycle_status=values["to_state"], config_version=self.record.config_version+1,
                              activated_at=(object() if values["to_state"]=="active" else self.record.activated_at),
                              disabled_at=(object() if values["to_state"]=="disabled" else self.record.disabled_at),
                              retired_at=(object() if values["to_state"]=="retired" else self.record.retired_at))
        return self.record
    def eligible_capability_count(self, instance_id): return self.eligible
    def has_effective_routing_reference(self, instance_id): return self.route
    def has_operational_history(self, instance_id): return self.history
    def delete_unused_draft(self, instance_id, expected_config_version): self.deleted=True; return True


def test_activation_requires_a_verified_capability_and_disable_is_reversible():
    repo=Repo(); service=ProviderInstanceService(repo)
    with pytest.raises(ProviderInstanceStateError, match="at least one"):
        service.activate(1, actor_user_id=7)
    repo.eligible=1
    assert service.activate(1, actor_user_id=7).lifecycle_status == "active"
    assert service.begin_draining(1, actor_user_id=7, reason="maintenance").lifecycle_status == "draining"
    assert service.disable(1, actor_user_id=7, reason="maintenance").lifecycle_status == "disabled"
    assert service.activate(1, actor_user_id=7).lifecycle_status == "active"


def test_retirement_requires_disabled_state_and_no_effective_route():
    repo=Repo("disabled"); service=ProviderInstanceService(repo); repo.route=True
    with pytest.raises(ProviderInstanceStateError, match="effective routing"):
        service.retire(1, actor_user_id=7, reason="end of service")
    repo.route=False
    assert service.retire(1, actor_user_id=7, reason="end of service").lifecycle_status == "retired"


@pytest.mark.parametrize("route,history", [(True,False),(False,True)])
def test_draft_with_route_or_operational_history_cannot_be_discarded(route, history):
    repo=Repo(); repo.route=route; repo.history=history
    with pytest.raises(ProviderInstanceStateError, match="cannot be discarded"):
        ProviderInstanceService(repo).discard_unused_draft(1)


def test_only_never_activated_unused_draft_can_be_discarded():
    repo=Repo(); ProviderInstanceService(repo).discard_unused_draft(1); assert repo.deleted
    repo=Repo("validation_failed"); repo.record=replace(repo.record, activated_at=object())
    with pytest.raises(ProviderInstanceStateError, match="never-activated"):
        ProviderInstanceService(repo).discard_unused_draft(1)
