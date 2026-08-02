"""Explicit registration list for code-owned external-data Adapters."""

from __future__ import annotations

from ..registry import DataRoutingRegistry
from .catalog import register_catalog
from .production_transports import bind_production_transports


def register_all(registry: DataRoutingRegistry) -> None:
    """Register every production Adapter and Capability in deterministic order."""

    register_catalog(registry)
    bind_production_transports()
