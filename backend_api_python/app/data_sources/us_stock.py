"""US equity data source facade backed by unified routing."""
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource


class USStockDataSource(BaseDataSource):
    """美股数据源"""
    
    name = "USStock/yfinance"
    
    INTERVAL_MAP = {
        '1m': '1m',
        '3m': '1m',
        '5m': '5m',
        '15m': '15m',
        '30m': '30m',
        '1H': '1h',
        '4H': '4h',
        '1D': '1d',
        '1W': '1wk'
    }
    
    DAYS_MAP = {
        '1m': lambda limit: min(7, max(1, (limit // 390) + 2)),
        '3m': lambda limit: min(7, max(1, (limit // 130) + 2)),
        '5m': lambda limit: min(60, max(1, (limit // 78) + 2)),
        '15m': lambda limit: min(60, max(2, (limit // 26) + 3)),
        '30m': lambda limit: min(60, max(2, (limit // 13) + 3)),
        '1H': lambda limit: min(730, max(5, int(limit / 6.5 * 7 / 5 * 1.5) + 5)),
        '4H': lambda limit: min(730, max(10, int(limit / 1.625 * 7 / 5 * 1.5) + 5)),
        '1D': lambda limit: min(3650, limit + 1),
        '1W': lambda limit: min(3650, (limit * 7) + 7)
    }

    MERGE_FACTOR_MAP = {
        '3m': 3,
    }

    TIMEFRAME_ALIASES = {
        '1h': '1H',
        '1hour': '1H',
        '60m': '1H',
        '4h': '4H',
        '1d': '1D',
        '1day': '1D',
        'd': '1D',
        '1w': '1W',
        '1wk': '1W',
        'w': '1W',
    }

    NASDAQ_HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/market-activity/stocks",
    }
    
    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取美股实时报价。"""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        symbol = (symbol or '').strip().upper()
        return dict(get_routed_external_data_gateway().execute(
            "market.us_stock.quote_kline",
            {"operation": "ticker", "market": "USStock", "symbol": symbol},
            constraints={"market": "USStock"},
        ).data or {})

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取美股K线数据"""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        result = get_routed_external_data_gateway().execute(
            "market.us_stock.quote_kline",
            {"operation": "kline", "market": "USStock", "symbol": symbol, "limit": limit, "before_time": before_time},
            constraints={"market": "USStock", "timeframe": timeframe},
        )
        return self.filter_and_limit(list(result.data or []), limit, before_time, after_time, truncate=after_time is None)
