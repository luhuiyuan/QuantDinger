"""Futures data source facade backed by unified routing."""
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource


class FuturesDataSource(BaseDataSource):
    """期货数据源"""
    
    name = "Futures"
    
    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get the latest futures ticker through the unified routing policy."""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        sym = (symbol or "").strip()
        return dict(get_routed_external_data_gateway().execute(
            "market.futures.quote_kline",
            {"operation": "ticker", "market": "Futures", "symbol": sym},
            constraints={"market": "Futures"},
        ).data or {})

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取期货K线数据
        
        Args:
            symbol: 期货合约代码
            timeframe: 时间周期
            limit: 数据条数
            before_time: 结束时间戳
            after_time: 预留与基类一致（当前期货链路未使用）
        """
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        result = get_routed_external_data_gateway().execute(
            "market.futures.quote_kline",
            {"operation": "kline", "market": "Futures", "symbol": symbol, "limit": limit, "before_time": before_time},
            constraints={"market": "Futures", "timeframe": timeframe},
        )
        return self.filter_and_limit(list(result.data or []), limit, before_time, after_time, truncate=after_time is None)
