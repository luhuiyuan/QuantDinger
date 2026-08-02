from __future__ import annotations

from types import SimpleNamespace

from app.data_sources.cn_stock import CNStockDataSource
from app.data_sources.hk_stock import HKStockDataSource
from app.services.market import cn_stock_market


class Gateway:
    def __init__(self): self.calls = []
    def execute(self, feature, subject, **kwargs):
        self.calls.append((feature, subject, kwargs))
        if "kline" in feature:
            return SimpleNamespace(data=[{"time": 1, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3}])
        return SimpleNamespace(data={"last": 2, "symbol": subject["symbol"]})


def test_cn_and_hk_sources_have_one_router_entry_without_local_provider_fallback(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr("app.data_sources.cn_stock.get_routed_external_data_gateway", lambda: gateway)
    monkeypatch.setattr("app.data_sources.hk_stock.get_routed_external_data_gateway", lambda: gateway)

    assert CNStockDataSource().get_ticker("600000")["last"] == 2
    assert HKStockDataSource().get_kline("00700", "1D", 10)[0]["close"] == 2
    assert [call[0] for call in gateway.calls] == ["market.cn_hk.quote_snapshot", "market.asia_stock.kline"]


def test_cn_market_business_exports_use_gateway_once(monkeypatch):
    gateway = Gateway()
    gateway.execute = lambda feature, subject, **kwargs: (
        gateway.calls.append((feature, subject, kwargs))
        or SimpleNamespace(data={"rows": [{"symbol": "600000.SH"}], "source": "akshare"})
    )
    monkeypatch.setattr(cn_stock_market, "get_routed_external_data_gateway", lambda: gateway)

    assert cn_stock_market.fetch_cn_market_snapshot()["rows"]
    assert [call[0] for call in gateway.calls] == ["market.cn_stock.snapshot"]


def test_cn_quote_and_index_exports_do_not_fallback_locally(monkeypatch):
    gateway = Gateway()
    responses = iter((
        SimpleNamespace(data=[{"symbol": "600000.SH"}]),
        SimpleNamespace(data=[{"symbol": "000001.SH", "status": "available"}]),
    ))
    gateway.execute = lambda feature, subject, **kwargs: (
        gateway.calls.append((feature, subject, kwargs)) or next(responses)
    )
    monkeypatch.setattr(cn_stock_market, "get_routed_external_data_gateway", lambda: gateway)

    assert cn_stock_market.fetch_cn_quote_rows(["600000"])[0]["symbol"] == "600000.SH"
    assert cn_stock_market.fetch_core_indices()[0]["status"] == "available"
    assert [call[0] for call in gateway.calls] == [
        "market.cn_hk.quote_snapshot",
        "market.cn_stock.core_indices",
    ]
