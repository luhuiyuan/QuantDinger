"""Hong Kong equity compatibility surface backed by unified data routing."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.services.data_routing.gateway import get_routed_external_data_gateway


class HKStockDataSource(BaseDataSource):
    name = "HKStock/unified-router"

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        result = get_routed_external_data_gateway().execute(
            "market.cn_hk.quote_snapshot",
            {"market": "HKStock", "symbol": symbol},
            constraints={"market": "HKStock"},
        )
        return dict(result.data or {})

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        result = get_routed_external_data_gateway().execute(
            "market.asia_stock.kline",
            {"market": "HKStock", "symbol": symbol, "limit": limit, "before_time": before_time},
            constraints={"market": "HKStock", "timeframe": timeframe, "adjustment": "qfq"},
        )
        return self.filter_and_limit(
            list(result.data or []), limit=limit, before_time=before_time,
            after_time=after_time, truncate=after_time is None,
        )
