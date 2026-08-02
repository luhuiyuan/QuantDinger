from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.data_routing.background_stream import (
    BackgroundCapabilityStreamService,
    CapabilityStreamStopped,
)
from app.services.data_routing.snapshot import AttemptEligibility
from app.services.data_routing.router import RoutedDataUnavailableError
from tests.test_data_router import Clock, Runtime, build_router


def test_background_stream_pins_revision_and_instance_and_never_silently_switches():
    clock = Clock()
    first = Runtime(clock, [])
    second = Runtime(clock, [])
    router, repository = build_router({"first": (first, 0), "second": (second, 0)})
    service = BackgroundCapabilityStreamService(router.registry, router.snapshots)

    stream = service.pin(stream_id="run:price", capability_key="quote", checkpoint={"page": 4})
    repository.gates[stream.instance_id] = AttemptEligibility(False, "quarantined")

    assert stream.policy_revision_id == 10 and stream.instance_id == 1
    with pytest.raises(CapabilityStreamStopped) as caught:
        service.before_next_chunk(stream, units=10, deadline=clock() + timedelta(minutes=1))
    assert caught.value.details["instance_id"] == 1
    assert stream.entry.instance_id != 2


def test_router_executes_only_the_instance_fixed_by_background_stream():
    clock = Clock()
    first = Runtime(clock, [{"price": 1, "symbol": "600000"}])
    second = Runtime(clock, [{"price": 2, "symbol": "600000"}])
    router, repository = build_router({"first": (first, 0), "second": (second, 0)})
    service = BackgroundCapabilityStreamService(router.registry, router.snapshots)
    stream = service.pin(stream_id="run:daily", capability_key="quote", checkpoint={"page": 0})
    repository.gates[stream.instance_id] = AttemptEligibility(False, "quarantined")

    with pytest.raises(RoutedDataUnavailableError):
        router.execute(
            "quote",
            {"symbol": "600000"},
            calling_feature="task.test.daily",
            mode="background",
            pinned_revision=stream.pinned_revision,
            fixed_instance_id=stream.instance_id,
        )

    assert first.calls == 0
    assert second.calls == 0


def test_router_uses_fixed_background_instance_when_eligible():
    clock = Clock()
    first = Runtime(clock, [{"price": 1, "symbol": "600000"}])
    second = Runtime(clock, [{"price": 2, "symbol": "600000"}])
    router, _ = build_router({"first": (first, 0), "second": (second, 0)})
    stream = BackgroundCapabilityStreamService(router.registry, router.snapshots).pin(
        stream_id="run:daily", capability_key="quote", checkpoint={"page": 0}
    )

    result = router.execute(
        "quote",
        {"symbol": "600000"},
        calling_feature="task.test.daily",
        mode="background",
        pinned_revision=stream.pinned_revision,
        fixed_instance_id=stream.instance_id,
    )

    assert result.data["price"] == 1
    assert first.calls == 1
    assert second.calls == 0
