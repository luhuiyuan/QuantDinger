"""Single application entry point for all routed external-data calling features."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping

from .bootstrap import load_default_data_routing_registry
from .cache import RoutingCache
from .errors import DataRoutingError
from .health import PostgresHealthRepository, ProviderHealthManager
from .observability import PostgresRouterObservabilitySink
from .quota import PostgresQuotaRepository, QuotaManager
from .router import DataRequestMode, DataRouter, RoutedDataResult
from .background_stream import BackgroundCapabilityStreamService, PinnedCapabilityStream
from .router_repository import PostgresRouterSecretResolver
from .snapshot import PostgresRoutingSnapshotRepository, RoutingSnapshotManager
from .provenance_context import record_routed_result


class UnknownCallingFeature(DataRoutingError):
    code = "unknown_data_calling_feature"


CALLING_FEATURE_CAPABILITIES: Mapping[str, str] = {
    "analysis.sentiment.adanos": "us_equity_sentiment",
    "market.commodities.overview": "commodity_quote",
    "market.crypto.overview": "crypto_market_snapshot",
    "market.economic_calendar": "economic_calendar",
    "market.forex.overview": "forex_quote",
    "market.global_heatmap": "global_heatmap",
    "market.indices.overview": "market_index_quote",
    "market.macro.series": "macro_series",
    "analysis.opportunities": "equity_opportunity",
    "analysis.market_sentiment": "market_sentiment",
    "market.asia_stock.kline": "asia_equity_kline",
    "task.cn_fundamental_history.eastmoney": "cn_fundamental_history",
    "market.cn_hk.fundamentals": "cn_hk_fundamentals",
    "task.cn_fundamental_history.cninfo_verification": "cn_corporate_announcement",
    "market.crypto.kline": "crypto_public_market_data",
    "market.forex.quote_kline": "forex_market_data",
    "market.futures.quote_kline": "futures_market_data",
    "market.moex.quote_kline": "moex_market_data",
    "market.cn_hk.quote_snapshot": "cn_hk_quote",
    "market.us_stock.quote_kline": "us_equity_market_data",
    "ai_chat.macro_nonfarm_context": "us_macro_release",
    "quick_trade.public_price_conversion": "crypto_public_quote",
    "admin.data_source_connection_test": "us_equity_quote",
    "task.cn_history.provider_actions": "cn_corporate_actions",
    "task.cn_history.official_adjustments": "cn_official_adjustment_reference",
    "task.cn_history.daily_bars": "cn_equity_history",
    "analysis.us_fundamentals": "us_fundamentals",
    "market.cn_stock.snapshot": "cn_market_snapshot",
    "market.cn_stock.core_indices": "cn_hk_quote",
    "market.symbol_search": "market_catalog",
    "analysis.market_data_collection": "global_market_overview",
    "analysis.news_search": "analysis_search",
    "task.symbol_master_sync": "symbol_master",
    "market.symbol_name": "symbol_reference",
}


def build_production_data_router() -> DataRouter:
    registry = load_default_data_routing_registry()
    snapshots = RoutingSnapshotManager(PostgresRoutingSnapshotRepository())
    snapshots.refresh_if_changed(force=True)
    return DataRouter(
        registry,
        snapshots,
        PostgresRouterSecretResolver(),
        observability=PostgresRouterObservabilitySink(),
        cache=RoutingCache(),
        quota=QuotaManager(PostgresQuotaRepository()),
        health=ProviderHealthManager(PostgresHealthRepository()),
    )


@dataclass
class RoutedExternalDataGateway:
    router: DataRouter

    def execute(
        self,
        calling_feature: str,
        subject: Mapping[str, Any],
        *,
        constraints: Mapping[str, Any] | None = None,
        mode: DataRequestMode | str = DataRequestMode.INTERACTIVE,
        timeout_seconds: float | None = None,
    ) -> RoutedDataResult:
        capability = CALLING_FEATURE_CAPABILITIES.get(str(calling_feature or ""))
        if capability is None:
            raise UnknownCallingFeature(
                "Calling Feature is not registered for unified data routing",
                details={"calling_feature": calling_feature},
            )
        result = self.router.execute(
            capability,
            dict(subject),
            constraints=dict(constraints or {}),
            mode=mode,
            calling_feature=calling_feature,
            timeout_seconds=timeout_seconds,
        )
        record_routed_result(result)
        return result

    def pin_background_stream(
        self,
        calling_feature: str,
        *,
        stream_id: str,
        checkpoint: Mapping[str, Any],
        instance_id: int | None = None,
    ) -> PinnedCapabilityStream:
        capability = CALLING_FEATURE_CAPABILITIES.get(str(calling_feature or ""))
        if capability is None:
            raise UnknownCallingFeature(
                "Calling Feature is not registered for unified data routing",
                details={"calling_feature": calling_feature},
            )
        return BackgroundCapabilityStreamService(
            self.router.registry,
            self.router.snapshots,
        ).pin(
            stream_id=stream_id,
            capability_key=capability,
            checkpoint=checkpoint,
            instance_id=instance_id,
        )

    def execute_background_chunk(
        self,
        calling_feature: str,
        stream: PinnedCapabilityStream,
        subject: Mapping[str, Any],
        *,
        constraints: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> RoutedDataResult:
        capability = CALLING_FEATURE_CAPABILITIES.get(str(calling_feature or ""))
        if capability != stream.capability_key:
            raise UnknownCallingFeature(
                "Calling Feature does not match the pinned Capability stream",
                details={"calling_feature": calling_feature, "capability_key": stream.capability_key},
            )
        result = self.router.execute(
            capability,
            dict(subject),
            constraints=dict(constraints or {}),
            mode=DataRequestMode.BACKGROUND,
            calling_feature=calling_feature,
            timeout_seconds=timeout_seconds,
            pinned_revision=stream.pinned_revision,
            fixed_instance_id=stream.instance_id,
        )
        record_routed_result(result)
        return result


_gateway_lock = RLock()
_gateway: RoutedExternalDataGateway | None = None


def get_routed_external_data_gateway(*, refresh_snapshot: bool = True) -> RoutedExternalDataGateway:
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = RoutedExternalDataGateway(build_production_data_router())
        elif refresh_snapshot:
            _gateway.router.snapshots.refresh_if_changed()
        return _gateway


def reset_routed_external_data_gateway() -> None:
    global _gateway
    with _gateway_lock:
        _gateway = None
