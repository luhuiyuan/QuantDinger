from __future__ import annotations

from types import SimpleNamespace

from app.services.market import symbol_search
from app.services import symbol_name
from app.services import symbol_master_sync


class Gateway:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def execute(self, feature, subject, **kwargs):
        self.calls.append((feature, subject, kwargs))
        return SimpleNamespace(data=self.data)


def test_symbol_search_external_miss_uses_one_gateway_call(monkeypatch):
    gateway = Gateway([{"market": "USStock", "symbol": "AAPL", "name": "Apple"}])
    monkeypatch.setattr(symbol_search, "get_routed_external_data_gateway", lambda: gateway, raising=False)
    monkeypatch.setattr(
        "app.services.data_routing.gateway.get_routed_external_data_gateway",
        lambda: gateway,
    )

    rows = symbol_search._routed_external_symbols("USStock", "AAPL", 10)

    assert rows[0]["symbol"] == "AAPL"
    assert gateway.calls[0][0] == "market.symbol_search"


def test_symbol_name_external_resolution_uses_gateway(monkeypatch):
    gateway = Gateway("Apple Inc.")
    monkeypatch.setattr(symbol_name, "seed_get_symbol_name", lambda *_args: None)
    monkeypatch.setattr(symbol_name, "persist_seed_name", lambda *_args: None)
    monkeypatch.setattr(
        "app.services.data_routing.gateway.get_routed_external_data_gateway",
        lambda: gateway,
    )

    assert symbol_name.resolve_symbol_name("USStock", "AAPL") == "Apple Inc."
    assert gateway.calls[0][0] == "market.symbol_name"


def test_dynamic_symbol_master_market_uses_gateway(monkeypatch):
    gateway = Gateway([{
        "market": "CNStock", "symbol": "600000", "name": "Pudong Bank",
        "exchange": "CN", "currency": "CNY",
    }])
    monkeypatch.setattr(
        "app.services.data_routing.gateway.get_routed_external_data_gateway",
        lambda: gateway,
    )
    monkeypatch.setattr(symbol_master_sync, "upsert_symbol_master", lambda rows: len(rows))

    result = symbol_master_sync.sync_symbol_master(["CNStock"])

    assert result["CNStock"]["upserted"] == 1
    assert gateway.calls[0][0] == "task.symbol_master_sync"
