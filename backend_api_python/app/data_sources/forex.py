"""Forex routing facade and shared provider symbol maps."""
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource


def normalize_forex_pair_symbol(symbol: str) -> str:
    """EUR/USD、XAU/USD、XAU-USD -> EURUSD、XAUUSD，供 Tiingo SYMBOL_MAP 与内部缓存键一致。"""
    if not symbol:
        return symbol
    s = str(symbol).strip().upper().replace(" ", "").replace("-", "")
    s = s.replace("/", "")
    aliases = {
        "XAU": "XAUUSD",
        "GOLD": "XAUUSD",
        "XAG": "XAGUSD",
        "SILVER": "XAGUSD",
    }
    return aliases.get(s, s)


_TD_SYMBOL_MAP = {
    'XAUUSD': 'XAU/USD', 'XAGUSD': 'XAG/USD',
    'EURUSD': 'EUR/USD', 'GBPUSD': 'GBP/USD', 'USDJPY': 'USD/JPY',
    'AUDUSD': 'AUD/USD', 'USDCAD': 'USD/CAD', 'USDCHF': 'USD/CHF',
    'NZDUSD': 'NZD/USD', 'GBPJPY': 'GBP/JPY', 'EURJPY': 'EUR/JPY',
    'EURGBP': 'EUR/GBP', 'AUDNZD': 'AUD/NZD', 'USDCNH': 'USD/CNH',
}

_YF_SYMBOL_MAP = {
    'XAUUSD': 'GC=F', 'XAGUSD': 'SI=F',
    'EURUSD': 'EURUSD=X', 'GBPUSD': 'GBPUSD=X', 'USDJPY': 'USDJPY=X',
    'AUDUSD': 'AUDUSD=X', 'USDCAD': 'USDCAD=X', 'USDCHF': 'USDCHF=X',
    'NZDUSD': 'NZDUSD=X', 'GBPJPY': 'GBPJPY=X', 'EURJPY': 'EURJPY=X',
    'EURGBP': 'EURGBP=X', 'USDCNH': 'USDCNH=X',
}

def _td_forex_symbol(symbol: str) -> str:
    """Convert internal symbol (e.g. EURUSD) to Twelve Data format (EUR/USD)."""
    s = symbol.upper().strip()
    if s in _TD_SYMBOL_MAP:
        return _TD_SYMBOL_MAP[s]
    if "/" in s:
        return s
    if len(s) == 6:
        return f"{s[:3]}/{s[3:]}"
    return s


class ForexDataSource(BaseDataSource):
    """Forex data source facade backed by the unified routing policy."""

    name = "Forex/Routed"
    
    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取外汇实时报价。"""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        symbol = normalize_forex_pair_symbol(symbol)
        return dict(get_routed_external_data_gateway().execute(
            "market.forex.quote_kline",
            {"operation": "ticker", "market": "Forex", "symbol": symbol},
            constraints={"market": "Forex"},
        ).data or {})

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取外汇K线数据。"""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        symbol = normalize_forex_pair_symbol(symbol)
        result = get_routed_external_data_gateway().execute(
            "market.forex.quote_kline",
            {"operation": "kline", "market": "Forex", "symbol": symbol, "limit": limit, "before_time": before_time},
            constraints={"market": "Forex", "timeframe": timeframe},
        )
        return self.filter_and_limit(list(result.data or []), limit, before_time, after_time=after_time, truncate=after_time is None)
