from __future__ import annotations

from types import SimpleNamespace

from app.data_sources.crypto import CryptoDataSource
from app.data_sources.forex import ForexDataSource
from app.data_sources.futures import FuturesDataSource
from app.data_sources.us_stock import USStockDataSource
from app.services.market_data_collector import MarketDataCollector
from app.data_providers.economic_calendar import get_economic_calendar_payload
from app.data_providers.macro_series import MacroSeriesProvider
from app.data_providers.commodities import fetch_commodities
from app.data_providers.crypto import fetch_crypto_prices
from app.data_providers.forex import fetch_forex_pairs
from app.data_providers.indices import fetch_stock_indices
from app.data_providers.adanos_sentiment import fetch_adanos_market_sentiment
from app.data_providers.heatmap import generate_heatmap_data
from app.data_providers.news import fetch_financial_news
from app.services.global_market_data import compute_market_sentiment, compute_trading_opportunities
from app.services.search import SearchService
from app.routes.ai_chat import _macro_release_lookup
from app.routes.quick_trade import _convert_usdt_to_base_qty


class Gateway:
    def __init__(self): self.calls = []
    def execute(self, feature, subject, **kwargs):
        self.calls.append((feature, subject, kwargs))
        data = [{"time": 1, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3}] if subject.get("operation") == "kline" else {"last": 2}
        return SimpleNamespace(data=data)


def test_global_market_data_sources_use_only_unified_gateway(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    assert USStockDataSource().get_ticker("AAPL")["last"] == 2
    assert CryptoDataSource().get_kline("BTC/USDT", "1D", 10)[0]["close"] == 2
    assert ForexDataSource().get_ticker("EUR/USD")["last"] == 2
    assert FuturesDataSource().get_kline("GC", "1D", 10)[0]["close"] == 2
    assert [item[0] for item in gateway.calls] == [
        "market.us_stock.quote_kline",
        "market.crypto.kline",
        "market.forex.quote_kline",
        "market.futures.quote_kline",
    ]


def test_us_fundamentals_use_unified_gateway(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    assert MarketDataCollector()._get_us_fundamental("AAPL")["last"] == 2
    assert gateway.calls[0][0] == "analysis.us_fundamentals"


def test_macro_and_calendar_use_unified_gateway(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    MacroSeriesProvider().fetch_fred_series("DGS10")
    MacroSeriesProvider().fetch_bls_series(["CUUR0000SA0"], 2025, 2026)
    MacroSeriesProvider().fetch_bea_data("NIPA")
    get_economic_calendar_payload()

    assert [call[0] for call in gateway.calls] == [
        "market.macro.series", "market.macro.series", "market.macro.series", "market.economic_calendar",
    ]


def test_global_overview_fetchers_use_unified_gateway(monkeypatch):
    gateway = Gateway()
    gateway.execute = lambda feature, subject, **kwargs: gateway.calls.append((feature, subject, kwargs)) or SimpleNamespace(data=[])
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    fetch_commodities()
    fetch_crypto_prices()
    fetch_forex_pairs()
    fetch_stock_indices()

    assert [call[0] for call in gateway.calls] == [
        "market.commodities.overview", "market.crypto.overview", "market.forex.overview", "market.indices.overview",
    ]


def test_analysis_and_heatmap_features_use_unified_gateway(monkeypatch):
    gateway = Gateway()
    def execute(feature, subject, **kwargs):
        gateway.calls.append((feature, subject, kwargs))
        if feature == "analysis.news_search" and subject.get("operation") == "search":
            return SimpleNamespace(data={"provider": "test", "results": [{"title": "t", "url": "https://example.test", "snippet": "s", "source": "test"}]}, provider_public_name="test")
        if feature == "analysis.news_search":
            return SimpleNamespace(data={"cn": [], "en": []}, provider_public_name="test")
        if feature in {"analysis.opportunities"}:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data={})
    gateway.execute = execute
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    generate_heatmap_data()
    compute_market_sentiment()
    compute_trading_opportunities()
    fetch_financial_news("en")
    fetch_adanos_market_sentiment("AAPL")
    response = object.__new__(SearchService).search_with_fallback("market news")

    assert response.success is True
    assert [call[0] for call in gateway.calls] == [
        "market.global_heatmap", "analysis.market_sentiment", "analysis.opportunities",
        "analysis.news_search", "analysis.sentiment.adanos", "analysis.news_search",
    ]


def test_quick_trade_public_price_conversion_uses_unified_gateway(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    quantity = _convert_usdt_to_base_qty(SimpleNamespace(), "BTC/USDT", 100.0, "spot")

    assert quantity == 50.0
    assert gateway.calls == [(
        "quick_trade.public_price_conversion",
        {"operation": "ticker", "symbol": "BTC/USDT"},
        {"constraints": {"exchange_id": "binance", "market_type": "spot"}},
    )]


def test_ai_nonfarm_context_uses_one_unified_capability(monkeypatch):
    gateway = Gateway()
    gateway.execute = lambda feature, subject, **kwargs: (
        gateway.calls.append((feature, subject, kwargs))
        or SimpleNamespace(data={"status": "ok", "actual": 175}, provider_public_name="BLS")
    )
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)

    result = _macro_release_lookup("latest nonfarm payrolls", {"indicator": "US_NONFARM_PAYROLLS"}, [], {})

    assert result["actual"] == 175
    assert result["answerable"] is True
    assert gateway.calls[0][0] == "ai_chat.macro_nonfarm_context"


def test_crypto_analysis_datasets_use_unified_gateway(monkeypatch):
    gateway = Gateway()
    gateway.execute = lambda feature, subject, **kwargs: (
        gateway.calls.append((feature, subject, kwargs))
        or SimpleNamespace(data={"source": "routed", "volume_24h": 10})
    )
    monkeypatch.setattr("app.services.data_routing.gateway.get_routed_external_data_gateway", lambda: gateway)
    collector = MarketDataCollector()

    collector._get_crypto_market_structure("BTC", {}, [])
    collector._get_crypto_derivatives_metrics("BTC")
    collector._get_crypto_capital_flow("BTC")

    assert [call[1]["operation"] for call in gateway.calls] == [
        "crypto_market_structure", "crypto_derivatives", "crypto_capital_flow",
    ]
    assert all(call[0] == "analysis.market_data_collection" for call in gateway.calls)
