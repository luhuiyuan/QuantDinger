"""Process-local bootstrap for the default data-routing registry."""

from __future__ import annotations

from threading import Lock

from .adapters import register_all
from .registry import DataRoutingRegistry, default_data_routing_registry


_load_lock = Lock()
_loaded = False


def load_default_data_routing_registry() -> DataRoutingRegistry:
    """Load and freeze the explicit production registry exactly once per process."""

    global _loaded
    if _loaded:
        return default_data_routing_registry
    with _load_lock:
        if not _loaded:
            register_all(default_data_routing_registry)
            default_data_routing_registry.freeze()
            _loaded = True
    return default_data_routing_registry
