import pytest

from app.services.task_control.registry import TaskDefinition, TaskRegistry


def _handler(**kwargs):
    return kwargs


def test_registry_validates_defaults_and_parameters():
    definition = TaskDefinition(
        task_key="catalog.sync",
        definition_version="2026-07-31.1",
        handler=_handler,
        default_parameters={"market": "CN"},
        parameter_schema={
            "type": "object",
            "required": ["market"],
            "properties": {"market": {"type": "string", "enum": ["CN", "US"]}},
            "additionalProperties": False,
        },
    )

    assert definition.validate_parameters({}) == {"market": "CN"}
    assert definition.exclusivity_key({}) == "catalog.sync"

    with pytest.raises(ValueError, match="unknown fields"):
        definition.validate_parameters({"unexpected": True})


def test_registry_rejects_duplicate_key_with_different_version():
    registry = TaskRegistry()
    registry.register(TaskDefinition("task.one", "1", _handler))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(TaskDefinition("task.one", "2", _handler))


def test_exclusivity_key_builder_receives_owner_and_validated_parameters():
    definition = TaskDefinition(
        task_key="agent.backtest",
        definition_version="1",
        handler=_handler,
        parameter_schema={"type": "object", "properties": {"symbol": {"type": "string"}}},
        exclusivity_key_builder=lambda params, owner: f"agent:{owner}:{params['symbol']}",
    )

    assert definition.exclusivity_key({"symbol": "600000"}, 7) == "agent:7:600000"


def test_registry_rejects_non_callable_shell_python_or_sql_entrypoints():
    for entrypoint in ("/bin/sh -c whoami", "module:function", "SELECT * FROM qd_users"):
        with pytest.raises(TypeError, match="code callable"):
            TaskDefinition("unsafe.task", "1", entrypoint)  # type: ignore[arg-type]
