from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.data_routing.adapters.catalog import AdapterTransportUnavailable, CatalogAdapterRuntime
from app.services.data_routing.bootstrap import load_default_data_routing_registry


def test_production_registry_covers_every_inventory_adapter_and_capability():
    root = Path(__file__).resolve().parents[2]
    inventory = json.loads((root / "docs/architecture/EXTERNAL_DATA_OUTBOUND_INVENTORY.json").read_text())
    registry = load_default_data_routing_registry()
    expected_adapters = {key for item in inventory["entries"] if item["scope"] == "in_scope" for key in item["adapters"]}
    expected_capabilities = {key for item in inventory["entries"] if item["scope"] == "in_scope" for key in item["capabilities"]}

    assert expected_adapters.issubset(registry.adapters)
    assert expected_capabilities.issubset(registry.capabilities)
    assert "easy_tdx" in registry.adapters
    assert registry.capabilities["cn_equity_history"].freshness_contract["storage_class"] == "persistent_dataset"


def test_unbound_catalog_transport_fails_closed_without_legacy_fallback():
    runtime = CatalogAdapterRuntime("not_bound")
    with pytest.raises(AdapterTransportUnavailable):
        runtime.fetch("us_equity_quote", {"symbol": "AAPL"}, {}, {}, {}, None)
