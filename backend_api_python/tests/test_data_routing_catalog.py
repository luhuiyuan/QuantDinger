from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.data_routing.adapters.catalog import (
    _DIAGNOSTIC_REQUESTS,
    AdapterTransportUnavailable,
    CatalogAdapterRuntime,
    bind_adapter_transport,
)
from app.services.cn_market_history.instruments import parse_cn_instrument
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


def test_catalog_diagnostic_uses_bounded_representative_request():
    captured = {}

    def transport(capability, subject, constraints, config, credentials, deadline):
        captured.update(capability=capability, subject=subject, constraints=constraints)
        return {"last": 1}

    bind_adapter_transport("diagnostic_fixture", transport)

    result = CatalogAdapterRuntime("diagnostic_fixture").diagnose(
        "asia_equity_kline", {"operation": "credential_validation"}, {}, {},
    )

    assert result["ok"] is True
    assert captured["subject"]["symbol"] == "600000"
    assert captured["subject"]["limit"] == 5
    assert captured["constraints"]["timeframe"] == "1D"


def test_a_share_history_diagnostic_samples_use_parseable_instruments():
    for capability_key in ("cn_corporate_actions", "cn_equity_history", "cn_official_adjustment_reference"):
        subject, _ = _DIAGNOSTIC_REQUESTS[capability_key]
        assert parse_cn_instrument(subject["instrument"]).canonical == "CNStock:600000.SH"
