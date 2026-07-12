"""Process role and startup-boundary tests."""

from __future__ import annotations

import pytest

from app.runtime.roles import ProcessRole, current_process_role, strategy_commands_enabled


def test_process_role_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("QD_PROCESS_ROLE", raising=False)
    assert current_process_role() is ProcessRole.LEGACY
    assert strategy_commands_enabled() is False


def test_api_enables_durable_strategy_commands_by_default(monkeypatch):
    monkeypatch.setenv("QD_PROCESS_ROLE", "api")
    monkeypatch.delenv("STRATEGY_COMMANDS_ENABLED", raising=False)
    assert current_process_role() is ProcessRole.API
    assert strategy_commands_enabled() is True


def test_invalid_process_role_fails_fast(monkeypatch):
    monkeypatch.setenv("QD_PROCESS_ROLE", "unknown")
    with pytest.raises(RuntimeError, match="Invalid QD_PROCESS_ROLE"):
        current_process_role()


def test_api_startup_does_not_launch_process_local_services(monkeypatch):
    from flask import Flask
    from app import startup

    monkeypatch.setenv("QD_PROCESS_ROLE", "api")
    monkeypatch.delenv("SKIP_STARTUP_HOOKS", raising=False)
    monkeypatch.setattr(startup, "_start_trading_support_services", lambda: pytest.fail("trading started"))
    monkeypatch.setattr(startup, "_start_scheduler_services", lambda **_: pytest.fail("scheduler started"))
    startup.run_startup_hooks(Flask(__name__))
