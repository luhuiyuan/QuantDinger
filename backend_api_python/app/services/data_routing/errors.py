"""Stable, transport-neutral data-routing errors."""

from __future__ import annotations

from typing import Any, Mapping


class DataRoutingError(Exception):
    """Base error carrying a stable code and sanitized structured details."""

    code = "data_routing_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})


class RegistryValidationError(DataRoutingError):
    code = "registry_validation_failed"


class RegistryConflictError(DataRoutingError):
    code = "registry_conflict"


class ConfigMigrationError(DataRoutingError):
    code = "provider_config_migration_failed"


class DataRoutingNotReadyError(DataRoutingError):
    code = "data_routing_not_ready"
