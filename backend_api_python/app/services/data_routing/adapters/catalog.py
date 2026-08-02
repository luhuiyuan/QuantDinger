"""Production Adapter and Capability catalog for every in-scope data domain."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Mapping

from ..errors import DataRoutingError
from ..models import AdapterDefinition, AdapterErrorClassification, AdapterFetchResult, CapabilityDefinition
from ..registry import DataRoutingRegistry


class AdapterTransportUnavailable(DataRoutingError):
    code = "adapter_transport_unavailable"


Transport = Callable[[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], datetime], Any]
_transport_lock = RLock()
_transports: dict[str, Transport] = {}


def bind_adapter_transport(adapter_key: str, transport: Transport) -> None:
    """Bind a trusted provider transport during application bootstrap or tests."""

    if not callable(transport) or not str(adapter_key or "").strip():
        raise ValueError("Adapter transport binding requires a key and callable")
    with _transport_lock:
        if adapter_key in _transports:
            raise ValueError(f"Adapter transport already bound: {adapter_key}")
        _transports[adapter_key] = transport


class CatalogAdapterRuntime:
    def __init__(self, adapter_key: str):
        self.adapter_key = adapter_key

    def resolve_account_identity(self, credentials, config):
        value = credentials.get("account_id") or config.get("account_id")
        return str(value).strip() if value else None

    def diagnose(self, capability_key, subject, config, credentials):
        result = self.fetch(capability_key, subject, {}, config, credentials, datetime.now(timezone.utc))
        return {"ok": result.payload is not None, "capability_key": capability_key}

    def normalize(self, capability_key, payload):
        return payload

    def supports_constraints(self, capability_key, constraints, config):
        supported = set(config.get("supported_constraints") or constraints.keys())
        return set(constraints).issubset(supported)

    def fetch(self, capability_key, subject, constraints, config, credentials, deadline):
        with _transport_lock:
            transport = _transports.get(self.adapter_key)
        if transport is None:
            raise AdapterTransportUnavailable(
                f"Adapter transport is not bound: {self.adapter_key}",
                details={"adapter_key": self.adapter_key, "capability_key": capability_key},
            )
        raw = transport(capability_key, subject, constraints, config, credentials, deadline)
        if isinstance(raw, AdapterFetchResult):
            return raw
        return AdapterFetchResult(raw, datetime.now(timezone.utc))

    def classify_error(self, capability_key, error):
        if isinstance(error, AdapterTransportUnavailable):
            return AdapterErrorClassification("adapter_not_configured", False, "permanent", "capability")
        name = type(error).__name__.lower()
        if "auth" in name or "permission" in name:
            return AdapterErrorClassification("authentication_rejected", False, "permanent", "instance")
        if "timeout" in name or "connection" in name:
            return AdapterErrorClassification("transport_error", True, "transient", "capability")
        return AdapterErrorClassification("provider_error", False, "unknown", "capability")

    def estimate_quota_cost(self, capability_key, operation, units):
        return {"requests": float(max(1, int(units)))}


_CAPABILITY_DOMAINS: dict[str, tuple[str, str]] = {
    "asia_equity_kline": ("kline", "CN_HK"),
    "cn_hk_quote": ("quote", "CN_HK"),
    "cn_market_snapshot": ("snapshot", "CN"),
    "cn_equity_history": ("history", "CN"),
    "cn_fundamental_history": ("fundamental", "CN"),
    "cn_hk_fundamentals": ("fundamental", "CN_HK"),
    "cn_corporate_announcement": ("corporate_action", "CN"),
    "cn_corporate_actions": ("corporate_action", "CN"),
    "cn_official_adjustment_reference": ("corporate_action", "CN"),
    "us_equity_market_data": ("market_data", "US"),
    "us_equity_quote": ("quote", "US"),
    "us_fundamentals": ("fundamental", "US"),
    "us_equity_sentiment": ("sentiment", "US"),
    "crypto_public_market_data": ("market_data", "CRYPTO"),
    "crypto_public_quote": ("quote", "CRYPTO"),
    "crypto_market_snapshot": ("snapshot", "CRYPTO"),
    "crypto_derivatives_market_data": ("derivatives", "CRYPTO"),
    "forex_market_data": ("market_data", "FX"),
    "forex_quote": ("quote", "FX"),
    "futures_market_data": ("market_data", "FUTURES"),
    "commodity_quote": ("quote", "GLOBAL"),
    "market_index_quote": ("quote", "GLOBAL"),
    "global_heatmap": ("snapshot", "GLOBAL"),
    "global_market_overview": ("snapshot", "GLOBAL"),
    "macro_series": ("macro", "GLOBAL"),
    "us_macro_release": ("macro", "US"),
    "economic_calendar": ("calendar", "GLOBAL"),
    "market_sentiment": ("sentiment", "GLOBAL"),
    "equity_opportunity": ("analysis", "GLOBAL"),
    "analysis_search": ("search", "GLOBAL"),
    "market_catalog": ("catalog", "GLOBAL"),
    "symbol_master": ("catalog", "GLOBAL"),
    "symbol_reference": ("reference", "GLOBAL"),
    "moex_market_data": ("market_data", "RU"),
}


_ADAPTER_CAPABILITIES: dict[str, frozenset[str]] = {
    "twelve_data": frozenset({"asia_equity_kline", "cn_hk_fundamentals", "us_equity_market_data", "forex_market_data", "forex_quote", "futures_market_data", "commodity_quote"}),
    "tencent": frozenset({"asia_equity_kline", "cn_hk_quote", "symbol_reference"}),
    "yfinance": frozenset({"asia_equity_kline", "us_equity_market_data", "us_fundamentals", "forex_market_data", "forex_quote", "futures_market_data", "commodity_quote", "market_index_quote", "global_heatmap", "crypto_market_snapshot", "market_sentiment", "symbol_reference"}),
    "akshare": frozenset({"asia_equity_kline", "cn_hk_fundamentals", "cn_market_snapshot", "economic_calendar", "market_sentiment", "us_macro_release", "market_catalog", "symbol_master"}),
    "easy_tdx": frozenset({"cn_equity_history", "cn_corporate_actions"}),
    "eastmoney": frozenset({"cn_fundamental_history"}),
    "cninfo": frozenset({"cn_corporate_announcement", "cn_official_adjustment_reference"}),
    "cn_exchange_official": frozenset({"cn_official_adjustment_reference"}),
    "finnhub": frozenset({"us_equity_market_data", "us_equity_quote", "economic_calendar", "equity_opportunity", "global_market_overview", "market_catalog", "symbol_master", "symbol_reference"}),
    "alpha_vantage": frozenset({"us_equity_market_data", "analysis_search"}),
    "tiingo": frozenset({"us_equity_market_data", "forex_market_data", "forex_quote", "futures_market_data", "commodity_quote"}),
    "ccxt_public_market": frozenset({"crypto_public_market_data", "crypto_derivatives_market_data", "market_catalog", "symbol_master"}),
    "binance_public": frozenset({"crypto_public_quote", "global_market_overview"}),
    "bybit_public": frozenset({"crypto_public_quote"}),
    "coingecko": frozenset({"crypto_market_snapshot", "global_market_overview"}),
    "coincap": frozenset({"crypto_market_snapshot"}),
    "coinglass": frozenset({"global_market_overview"}),
    "cryptoquant": frozenset({"global_market_overview"}),
    "fred": frozenset({"macro_series"}),
    "bls": frozenset({"macro_series", "us_macro_release"}),
    "bea": frozenset({"macro_series"}),
    "trading_economics": frozenset({"economic_calendar"}),
    "cnn_fear_greed": frozenset({"market_sentiment"}),
    "adanos": frozenset({"us_equity_sentiment"}),
    "searxng": frozenset({"analysis_search"}),
    "brave": frozenset({"analysis_search"}),
    "bing": frozenset({"analysis_search"}),
    "gdelt": frozenset({"analysis_search"}),
    "tavily": frozenset({"analysis_search"}),
    "jina": frozenset({"analysis_search"}),
    "moex": frozenset({"moex_market_data", "market_catalog", "symbol_master", "symbol_reference"}),
    "stooq": frozenset({"symbol_master"}),
    "nasdaq_trader": frozenset({"symbol_master"}),
}


_PERSISTENT_FAMILIES = frozenset({"history", "fundamental", "corporate_action", "catalog"})
_SECRET_ADAPTERS = frozenset({
    "twelve_data", "finnhub", "alpha_vantage", "tiingo", "fred", "bls", "bea",
    "trading_economics", "adanos", "brave", "bing", "tavily", "coinglass", "cryptoquant",
})


def _quality_gate(value: Any) -> tuple[str, ...]:
    if value is None or value == [] or value == {}:
        return ("empty_provider_result",)
    return ()


def _cache_fields(family: str) -> tuple[str, ...]:
    if family in {"kline", "history", "market_data", "derivatives"}:
        return ("subject", "timeframe", "adjustment", "start", "end")
    if family in {"quote", "snapshot"}:
        return ("subject", "venue", "market_type")
    if family == "macro":
        return ("subject", "series_id", "start", "end")
    if family == "search":
        return ("subject", "query")
    return ("subject", "as_of")


def register_catalog(registry: DataRoutingRegistry) -> None:
    allowed = frozenset({
        "symbol", "timeframe", "adjustment", "market", "venue", "max_delay", "start", "end",
        "limit", "series_id", "query", "exchange_id", "market_type", "currency", "as_of",
    })
    for key, (family, market) in sorted(_CAPABILITY_DOMAINS.items()):
        persistent = family in _PERSISTENT_FAMILIES
        registry.register_capability(CapabilityDefinition(
            key=key,
            version="1",
            public_name=key.replace("_", " ").title(),
            semantic_family=family,
            market=market,
            source_module=__name__,
            allowed_constraints=allowed,
            normalized_contract={"type": family, "version": 1},
            hard_quality_gates=(_quality_gate,),
            strict_profiles={"strict": {"reject_warnings": True}},
            cache_key_fields=_cache_fields(family),
            freshness_contract={
                "storage_class": "persistent_dataset" if persistent else "routing_cache",
                "fresh_seconds": 0 if persistent else (15 if family in {"quote", "snapshot"} else 300),
                "stale_seconds": 0 if persistent else 3600,
            },
            health_contract={"window_seconds": 300, "transient_failure_threshold": 3, "cooldown_seconds": 60},
        ))

    empty_schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    config_schema = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string"}, "timeout_seconds": {"type": "number"},
            "account_id": {"type": "string"}, "supported_constraints": {"type": "array"},
        },
        "required": [],
        "additionalProperties": False,
    }
    credential_schema = {
        "type": "object",
        "properties": {"api_key": {"type": "string"}, "api_secret": {"type": "string"}, "account_id": {"type": "string"}},
        "required": ["api_key"],
        "additionalProperties": False,
    }
    for key, capabilities in sorted(_ADAPTER_CAPABILITIES.items()):
        registry.register_adapter(AdapterDefinition(
            key=key,
            version="1",
            public_name=key.replace("_", " ").title(),
            source_module=__name__,
            config_schema=config_schema,
            credential_schema=credential_schema if key in _SECRET_ADAPTERS else empty_schema,
            capabilities=capabilities,
            quota_contract={
                "scope": "capability", "buckets": ["requests"], "unit": "request",
                "unknown_limit_safety_budget": 30, "reset_seconds": 60,
            },
            runtime=CatalogAdapterRuntime(key),
            transport_retry_limit=1,
        ))
