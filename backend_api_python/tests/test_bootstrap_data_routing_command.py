from __future__ import annotations

import json

import pytest

from app.commands.bootstrap_data_routing import main
from app.services.data_routing.default_bootstrap import DefaultRoutingBootstrapResult


class Service:
    def __init__(self):
        self.calls = []

    def bootstrap(self, **values):
        self.calls.append(values)
        return DefaultRoutingBootstrapResult(
            dry_run=values["dry_run"], planned_adapters=("public_a",),
        )


def test_command_emits_structured_summary_and_forwards_safe_options(capsys):
    service = Service()
    factories = []

    def factory(**values):
        factories.append(values)
        return service

    result = main(
        ["--dry-run", "--retry-failed", "--actor-user-id", "7", "--timeout-seconds", "3"],
        service_factory=factory,
    )

    assert result.dry_run is True
    assert factories == [{"dry_run": True, "timeout_seconds": 3}]
    assert service.calls == [{"actor_user_id": 7, "retry_failed": True, "dry_run": True}]
    assert json.loads(capsys.readouterr().out)["planned_adapters"] == ["public_a"]


def test_command_rejects_non_positive_actor_id():
    with pytest.raises(SystemExit, match="positive"):
        main(["--actor-user-id", "0"], service_factory=lambda **_: Service())
