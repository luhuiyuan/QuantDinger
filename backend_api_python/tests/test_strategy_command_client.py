"""Durable strategy command client tests."""

from __future__ import annotations

from dataclasses import replace

from app.services.strategy_command_client import StrategyCommandClient
from app.services.strategy_command_repository import StrategyCommand


class FakeRepository:
    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.commands = []

    def enqueue(self, *, strategy_id, command_type, **_kwargs):
        command = StrategyCommand(
            id=len(self.commands) + 1,
            strategy_id=int(strategy_id),
            user_id=0,
            command_type=command_type,
            status=self.status,
            idempotency_key=f"key-{len(self.commands) + 1}",
            payload={},
        )
        self.commands.append(command)
        return command

    def get(self, command_id):
        command = self.commands[int(command_id) - 1]
        return replace(command, status=self.status, error_message="executor failed")


def test_start_is_accepted_while_worker_is_processing():
    repository = FakeRepository(status="processing")
    client = StrategyCommandClient(repository)

    assert client.start_strategy(42) is True
    running, detail = client.wait_strategy_running(42, timeout=0)

    assert running is True
    assert detail == ""
    assert repository.commands[0].command_type == "start"


def test_start_reports_terminal_worker_failure():
    repository = FakeRepository(status="failed")
    client = StrategyCommandClient(repository)

    assert client.start_strategy(7) is True
    running, detail = client.wait_strategy_running(7, timeout=0)

    assert running is False
    assert detail == "executor failed"


def test_stop_uses_durable_command(monkeypatch):
    repository = FakeRepository(status="succeeded")
    client = StrategyCommandClient(repository)
    monkeypatch.setenv("STRATEGY_COMMAND_STOP_WAIT_SEC", "0")

    assert client.stop_strategy(9, persist_status=False) is True
    assert repository.commands[0].command_type == "stop"
