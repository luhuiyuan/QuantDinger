from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.data_routing.gateway import (
    CALLING_FEATURE_CAPABILITIES,
    RoutedExternalDataGateway,
    UnknownCallingFeature,
)
from app.services.data_routing.provenance_context import (
    clear_routed_result,
    record_routed_result,
    safe_provenance_headers,
)


class FakeRouter:
    def __init__(self): self.calls = []
    def execute(self, capability, subject, **kwargs):
        self.calls.append((capability, subject, kwargs))
        return {"data": [1]}


def test_every_inventory_calling_feature_has_one_gateway_router_entry():
    root = Path(__file__).resolve().parents[2]
    inventory = json.loads((root / "docs/architecture/EXTERNAL_DATA_OUTBOUND_INVENTORY.json").read_text())
    expected = {feature for item in inventory["entries"] if item["scope"] == "in_scope" for feature in item["calling_features"]}
    assert expected.issubset(CALLING_FEATURE_CAPABILITIES)


def test_every_inventory_calling_feature_has_a_versioned_compatibility_fixture():
    root = Path(__file__).resolve().parents[2]
    inventory = json.loads((root / "docs/architecture/EXTERNAL_DATA_OUTBOUND_INVENTORY.json").read_text())
    contracts = json.loads((Path(__file__).parent / "fixtures/routed_calling_feature_contracts.json").read_text())
    expected = {feature for item in inventory["entries"] if item["scope"] == "in_scope" for feature in item["calling_features"]}
    assert set(contracts) == expected
    for contract in contracts.values():
        assert isinstance(contract["request"], dict)
        assert contract["success_type"] in {"object", "array", "string"}
        assert contract["domain_error"]


def test_gateway_pins_capability_and_preserves_calling_feature_contract():
    router = FakeRouter()
    gateway = RoutedExternalDataGateway(router)
    result = gateway.execute("market.asia_stock.kline", {"symbol": "sh600000"}, constraints={"timeframe": "1d"})
    assert result == {"data": [1]}
    assert router.calls == [(
        "asia_equity_kline", {"symbol": "sh600000"}, {
            "constraints": {"timeframe": "1d"}, "mode": "interactive",
            "calling_feature": "market.asia_stock.kline", "timeout_seconds": None,
        },
    )]


def test_gateway_rejects_unregistered_feature_instead_of_falling_back():
    with pytest.raises(UnknownCallingFeature):
        RoutedExternalDataGateway(FakeRouter()).execute("legacy.unknown", {})


def test_safe_provenance_headers_expose_only_public_fields():
    clear_routed_result()
    record_routed_result(SimpleNamespace(
        routed_request_id="routed-123",
        provider_public_name="Public Provider",
        acquired_at=datetime(2026, 8, 1, 3, 4, 5, tzinfo=timezone.utc),
        freshness="stale",
        quality_warnings=(SimpleNamespace(code="partial"),),
        provenance=SimpleNamespace(
            provider_instance_id=42,
            policy_revision_id=99,
            attempts=("internal failure",),
        ),
    ))

    assert safe_provenance_headers() == {
        "X-Routed-Data-Request-ID": "routed-123",
        "X-Data-Provider": "Public Provider",
        "X-Data-Acquired-At": "2026-08-01T03:04:05Z",
        "X-Data-Freshness": "stale",
        "X-Data-Quality-Warning-Count": "1",
    }


def test_clearing_provenance_prevents_cross_request_leakage():
    record_routed_result(SimpleNamespace(
        routed_request_id="old", provider_public_name="Provider",
        acquired_at="2026-08-01T00:00:00Z", freshness="fresh",
        quality_warnings=(),
    ))
    clear_routed_result()
    assert safe_provenance_headers() == {}
