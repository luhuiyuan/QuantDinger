from __future__ import annotations

from types import SimpleNamespace

from app.services.cn_fundamental_history.routed_provider import RoutedCNFundamentalFetcher
from app.services.market_data_collector import MarketDataCollector


class Gateway:
    def __init__(self):
        self.calls = []

    def pin_background_stream(self, feature, **kwargs):
        self.calls.append(("pin", feature, kwargs))
        return SimpleNamespace(capability_key="cn_fundamental_history", instance_id=7)

    def execute_background_chunk(self, feature, stream, subject, **kwargs):
        self.calls.append(("chunk", feature, subject, kwargs))
        return SimpleNamespace(data=[{"period_end": "2025-12-31"}])

    def execute(self, feature, subject, **kwargs):
        self.calls.append(("execute", feature, subject, kwargs))
        if feature == "market.cn_hk.quote_snapshot":
            return SimpleNamespace(data={"last": 10, "previousClose": 9, "changePercent": 11.1})
        return SimpleNamespace(data={"pe_ratio": 12, "source": "twelve_data"})


def test_fundamental_fetcher_pins_once_and_executes_fixed_stream():
    gateway = Gateway()
    fetcher = RoutedCNFundamentalFetcher(gateway=gateway)
    fetcher.start_stream("run-1")

    assert fetcher("CNStock:600000.SH")[0]["period_end"] == "2025-12-31"
    assert [item[0] for item in gateway.calls] == ["pin", "chunk"]


def test_cn_hk_collector_merges_quote_and_one_routed_fundamental_result(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr(
        "app.services.data_routing.gateway.get_routed_external_data_gateway",
        lambda: gateway,
    )

    result = MarketDataCollector()._get_cn_hk_fundamental("CNStock", "600000")

    assert result["last"] == 10
    assert result["pe_ratio"] == 12
    assert [item[1] for item in gateway.calls] == [
        "market.cn_hk.quote_snapshot",
        "market.cn_hk.fundamentals",
    ]
