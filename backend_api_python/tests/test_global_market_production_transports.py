from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.services.data_routing.adapters.production_transports import (
    _binance_public,
    _bls,
    _ccxt_market_data,
    _finnhub,
    _tiingo,
    _yfinance,
)
from app.services.data_routing.bootstrap import load_default_data_routing_registry


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_finnhub_candles_normalize_and_merge_four_hours(monkeypatch):
    payload = {
        "t": [1, 2, 3, 4], "o": [1, 2, 3, 4], "h": [2, 3, 4, 5],
        "l": [0, 1, 2, 3], "c": [2, 3, 4, 5], "v": [1, 1, 1, 1],
    }
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: Response(payload))

    bars = _finnhub("us_equity_market_data", {"operation": "kline", "symbol": "AAPL", "limit": 1}, {"timeframe": "4h"}, {}, {"api_key": "x"}, None)

    assert bars == [{"time": 1, "open": 1.0, "high": 5.0, "low": 0.0, "close": 5.0, "volume": 4.0}]


def test_ccxt_swap_symbol_uses_settlement_suffix(monkeypatch):
    observed = {}

    class Exchange:
        def __init__(self, config): pass
        def fetch_ticker(self, symbol):
            observed["symbol"] = symbol
            return {"last": 1}

    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(binance=Exchange))
    monkeypatch.setattr("app.data_sources.crypto.resolve_ccxt_for_live_trading", lambda exchange, market: ("binance", {}))
    monkeypatch.setattr("app.data_sources.crypto.apply_public_ccxt_endpoint_config", lambda config, exchange: config)

    _ccxt_market_data("crypto_derivatives_market_data", {"operation": "ticker", "symbol": "BTC/USDT"}, {"market_type": "swap"}, {}, {}, None)

    assert observed["symbol"] == "BTC/USDT:USDT"


def test_tiingo_accepts_base_url_already_ending_in_fx(monkeypatch):
    observed = {}
    def get(url, **kwargs):
        observed["url"] = url
        return Response([{"midPrice": 1.2}])
    monkeypatch.setattr("requests.get", get)

    result = _tiingo("forex_quote", {"operation": "ticker", "symbol": "EUR/USD"}, {}, {"base_url": "https://api.tiingo.com/tiingo/fx"}, {"api_key": "x"}, None)

    assert observed["url"] == "https://api.tiingo.com/tiingo/fx/top"
    assert result["last"] == 1.2


def test_yfinance_four_hour_bars_are_merged(monkeypatch):
    class Frame:
        empty = False
        def iterrows(self):
            for index in range(8):
                stamp = SimpleNamespace(timestamp=lambda index=index: index + 1)
                yield stamp, {"Open": index + 1, "High": index + 2, "Low": index, "Close": index + 1.5, "Volume": 1}

    ticker = SimpleNamespace(history=lambda **kwargs: Frame())
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda symbol: ticker))

    bars = _yfinance("us_equity_market_data", {"operation": "kline", "symbol": "AAPL", "limit": 2}, {"timeframe": "4H"}, {}, {}, None)

    assert len(bars) == 2
    assert bars[0]["open"] == 1.0
    assert bars[0]["close"] == 4.5


def test_yfinance_us_fundamentals_are_normalized(monkeypatch):
    ticker = SimpleNamespace(info={"trailingPE": 24.5, "marketCap": 1000, "freeCashflow": 90})
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda symbol: ticker))

    result = _yfinance("us_fundamentals", {"symbol": "AAPL"}, {}, {}, {}, None)

    assert result["pe_ratio"] == 24.5
    assert result["market_cap"] == 1000
    assert result["free_cash_flow"] == 90
    assert result["source"] == "yfinance"


def test_bls_nonfarm_release_is_normalized(monkeypatch):
    payload = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"data": [
        {"year": "2026", "period": "M07", "value": "159000"},
        {"year": "2026", "period": "M06", "value": "158825"},
    ]}]}}
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: Response(payload))

    result = _bls("us_macro_release", {"indicator": "US_NONFARM_PAYROLLS"}, {}, {}, {}, None)

    assert result["actual"] == 175
    assert result["period"] == "2026-07"


def test_binance_public_quote_is_normalized(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: Response({"price": "123.45"}))

    result = _binance_public(
        "crypto_public_quote", {"symbol": "BTC/USDT"},
        {"market_type": "spot"}, {}, {}, None,
    )

    assert result == {"symbol": "BTC/USDT", "last": 123.45}


@pytest.mark.parametrize("capability", [
    "us_equity_market_data", "crypto_public_market_data", "crypto_derivatives_market_data",
    "forex_market_data", "futures_market_data",
])
def test_global_market_capabilities_declare_constraints_cache_and_health(capability):
    definition = load_default_data_routing_registry().capabilities[capability]
    assert {"market", "timeframe", "market_type"} <= definition.allowed_constraints
    assert definition.freshness_contract["storage_class"] == "routing_cache"
    assert definition.cache_key_fields
    assert definition.health_contract["transient_failure_threshold"] > 0


@pytest.mark.parametrize("adapter", ["finnhub", "yfinance", "ccxt_public_market", "twelve_data", "tiingo"])
def test_global_market_adapters_have_conservative_quota_contracts(adapter):
    definition = load_default_data_routing_registry().adapters[adapter]
    assert definition.quota_contract["unknown_limit_safety_budget"] > 0
    assert definition.transport_retry_limit <= 1
