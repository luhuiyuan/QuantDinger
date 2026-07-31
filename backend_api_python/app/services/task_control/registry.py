"""Code-owned registry for finite Task Definitions.

The registry deliberately accepts only Python-registered handlers. It is not a
plugin loader and never evaluates a user supplied import path or command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


TaskHandler = Callable[..., Any]
ExclusivityKeyBuilder = Callable[[Mapping[str, Any], int | None], str]


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_key: str
    definition_version: str
    handler: TaskHandler
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    default_parameters: dict[str, Any] = field(default_factory=dict)
    display_name: str = ""
    priority: int = 2
    capabilities: dict[str, Any] = field(default_factory=dict)
    exclusivity_key_builder: ExclusivityKeyBuilder | None = None

    def __post_init__(self) -> None:
        if not self.task_key or self.task_key.strip() != self.task_key:
            raise ValueError("task_key must be a non-empty stable identifier")
        if not self.definition_version:
            raise ValueError("definition_version is required")
        if not callable(self.handler):
            raise TypeError("Task Definition handler must be code callable")
        if self.priority not in {0, 1, 2, 3}:
            raise ValueError("priority must be between 0 and 3")

    def validate_parameters(self, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(self.default_parameters)
        merged.update(dict(parameters or {}))
        _validate_object(self.parameter_schema, merged, path="$" )
        return merged

    def exclusivity_key(self, parameters: Mapping[str, Any], owner_user_id: int | None = None) -> str:
        validated = self.validate_parameters(parameters)
        if self.exclusivity_key_builder is None:
            return self.task_key
        key = str(self.exclusivity_key_builder(validated, owner_user_id) or "").strip()
        if not key:
            raise ValueError(f"Task Definition {self.task_key} returned an empty exclusivity key")
        return key


class TaskRegistry:
    """Process-local code registry; database materialization is a separate concern."""

    def __init__(self) -> None:
        self._definitions: dict[str, TaskDefinition] = {}

    def register(self, definition: TaskDefinition) -> TaskDefinition:
        previous = self._definitions.get(definition.task_key)
        if previous is not None and previous.definition_version != definition.definition_version:
            raise ValueError(
                f"Task Definition already registered with version {previous.definition_version}: "
                f"{definition.task_key}"
            )
        self._definitions[definition.task_key] = definition
        return definition

    def get(self, task_key: str) -> TaskDefinition:
        try:
            return self._definitions[str(task_key)]
        except KeyError as exc:
            raise KeyError(f"Unknown Task Definition: {task_key}") from exc

    def all(self) -> tuple[TaskDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))


def _validate_object(schema: Mapping[str, Any], value: Any, *, path: str) -> None:
    if not schema:
        return
    if schema.get("type", "object") != "object" or not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    required = schema.get("required", [])
    for name in required:
        if name not in value:
            raise ValueError(f"{path}.{name} is required")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties", True) is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ValueError(f"{path} contains unknown fields: {', '.join(unknown)}")
    for name, child_schema in properties.items():
        if name in value:
            _validate_value(child_schema, value[name], path=f"{path}.{name}")


def _validate_value(schema: Mapping[str, Any], value: Any, *, path: str) -> None:
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
    }
    if expected and not valid.get(expected, True):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of the registered enum values")
    if expected == "object":
        _validate_object(schema, value, path=path)


default_task_registry = TaskRegistry()
