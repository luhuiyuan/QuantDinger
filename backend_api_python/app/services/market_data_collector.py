"""AI analysis market-data collection through unified routing facades."""

import time
from typing import Dict, List, Any, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from app.data_sources import DataSourceFactory
from app.services.kline import KlineService
from app.services.market.technical_indicators import calculate_indicators
from app.utils.logger import get_logger

logger = get_logger(__name__)


class NonBlockingThreadPoolExecutor(ThreadPoolExecutor):
    """Thread pool that does not wait for slow optional data providers on exit."""

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(wait=False, cancel_futures=True)
        return False


class MarketDataCollector:
    """
    市场数据采集器
    
    职责：为AI分析提供完整、准确、及时的市场数据
    
    数据层次：
    1. 核心数据 (必须成功): 价格、K线
    2. 分析数据 (增强): 技术指标、基本面
    3. 宏观数据 (可选): 复用 global_market.py (VIX, DXY, TNX, Fear&Greed等)
    4. 情绪数据 (可选): 新闻、市场情绪
    """
    
    def __init__(self):
        self.kline_service = KlineService()
        self._finnhub_client = None
        self._ak = None
        self._crypto_metric_cache: Dict[str, Dict[str, Any]] = {}
        self._init_clients()
    
    def _init_clients(self):
        """External data clients are resolved only inside registered Adapters."""
    
    def collect_all(
        self,
        market: str,
        symbol: str,
        timeframe: str = "1D",
        include_macro: bool = True,
        include_news: bool = True,
        timeout: int = 30
    ) -> Dict[str, Any]:
        """
        采集所有市场数据
        
        Args:
            market: 市场类型 (USStock, Crypto, Forex, Futures)
            symbol: 标的代码
            timeframe: K线周期
            include_macro: 是否包含宏观数据
            include_news: 是否包含新闻
            timeout: 总超时时间(秒)
            
        Returns:
            完整的市场数据字典
        """
        start_time = time.time()
        
        data = {
            "market": market,
            "symbol": symbol,
            "timeframe": timeframe,
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "price": None,
            "kline": None,
            "indicators": {},
            "fundamental": {},
            "company": {},
            "crypto_factors": {},
            "macro": {},
            "news": [],
            "sentiment": {},
            "_meta": {
                "success_items": [],
                "failed_items": [],
                "duration_ms": 0
            }
        }
        
        with NonBlockingThreadPoolExecutor(max_workers=4) as executor:
            core_futures = {
                executor.submit(self._get_price, market, symbol): "price",
                executor.submit(self._get_kline, market, symbol, timeframe, 60): "kline",
            }
            
            if market in ('USStock', 'CNStock', 'HKStock'):
                core_futures[executor.submit(self._get_fundamental, market, symbol)] = "fundamental"
                core_futures[executor.submit(self._get_company, market, symbol)] = "company"
            elif market == 'Crypto':
                core_futures[executor.submit(self._get_crypto_info, symbol)] = "fundamental"
            
            try:
                for future in as_completed(core_futures, timeout=15):
                    key = core_futures[future]
                    try:
                        result = future.result(timeout=3)
                        if result:
                            data[key] = result
                            data["_meta"]["success_items"].append(key)
                        else:
                            data["_meta"]["failed_items"].append(key)
                    except Exception as e:
                        logger.warning(f"Core data fetch failed ({key}): {e}")
                        data["_meta"]["failed_items"].append(key)
            except TimeoutError:
                logger.warning(f"Core data fetch timed out for {market}:{symbol}")
        
        if data.get("kline"):
            data["indicators"] = self._calculate_indicators(data["kline"])
            data["_meta"]["success_items"].append("indicators")

        if market == 'Crypto':
            try:
                data["crypto_factors"] = self._get_crypto_factors(
                    symbol=symbol,
                    price_data=data.get("price") or {},
                    kline_data=data.get("kline") or [],
                )
                if data["crypto_factors"]:
                    data["_meta"]["success_items"].append("crypto_factors")
                else:
                    data["_meta"]["failed_items"].append("crypto_factors")
            except Exception as e:
                logger.warning(f"Crypto factor fetch failed for {symbol}: {e}")
                data["_meta"]["failed_items"].append("crypto_factors")
        
        if include_macro:
            try:
                data["macro"] = self._get_macro_data(market, timeout=10)
                if data["macro"]:
                    data["_meta"]["success_items"].append("macro")
            except Exception as e:
                logger.warning(f"Macro data fetch failed: {e}")
                data["_meta"]["failed_items"].append("macro")
        
        if include_news:
            try:
                company_name = None
                if data.get("company"):
                    company_name = data["company"].get("name")
                
                news_result = self._get_news(market, symbol, company_name, timeout=8)
                data["news"] = news_result.get("news", [])
                data["sentiment"] = news_result.get("sentiment", {})
                
                if data["news"]:
                    data["_meta"]["success_items"].append("news")
            except Exception as e:
                logger.warning(f"News fetch failed: {e}")
                data["_meta"]["failed_items"].append("news")
        
        data["_meta"]["duration_ms"] = int((time.time() - start_time) * 1000)
        logger.info(f"Market data collection completed for {market}:{symbol} in {data['_meta']['duration_ms']}ms")
        logger.info(f"  Success: {data['_meta']['success_items']}")
        logger.info(f"  Failed: {data['_meta']['failed_items']}")
        
        return data
    
    
    def _get_price(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时价格 - 使用 kline_service (与自选列表一致)
        """
        try:
            price_data = self.kline_service.get_realtime_price(market, symbol, force_refresh=True)
            if price_data and price_data.get('price', 0) > 0:
                def safe_float(val, default=0.0):
                    if val is None:
                        return default
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        return default
                
                price = safe_float(price_data.get('price'))
                return {
                    "price": price,
                    "change": safe_float(price_data.get('change')),
                    "changePercent": safe_float(price_data.get('changePercent')),
                    "high": safe_float(price_data.get('high'), price),
                    "low": safe_float(price_data.get('low'), price),
                    "open": safe_float(price_data.get('open'), price),
                    "previousClose": safe_float(price_data.get('previousClose'), price),
                    "source": price_data.get('source', 'unknown')
                }
        except Exception as e:
            logger.warning(f"Price fetch failed for {market}:{symbol}: {e}")
        return None
    
    def _get_kline(
        self, market: str, symbol: str, timeframe: str, limit: int = 60
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取K线数据 - 使用 DataSourceFactory (与K线模块一致)
        """
        try:
            klines = DataSourceFactory.get_kline(market, symbol, timeframe, limit)
            if klines and len(klines) > 0:
                return klines
        except Exception as e:
            logger.warning(f"Kline fetch failed for {market}:{symbol}: {e}")
        return None
    
    def _calculate_indicators(self, klines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate local technical indicators for market analysis."""
        try:
            return calculate_indicators(klines)
        except Exception as e:
            logger.warning(f"Indicator calculation failed: {e}")
            return {}
    
    
    def _get_fundamental(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        """获取基本面数据"""
        try:
            if market == 'USStock':
                return self._get_us_fundamental(symbol)
            if market in ('CNStock', 'HKStock'):
                return self._get_cn_hk_fundamental(market, symbol)
        except Exception as e:
            logger.warning(f"Fundamental data fetch failed for {market}:{symbol}: {e}")
        return None

    def _get_cn_hk_fundamental(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch CN/HK quote and fundamentals through independent Capabilities."""
        try:
            from app.services.data_routing.gateway import get_routed_external_data_gateway

            gateway = get_routed_external_data_gateway()
            quote = gateway.execute(
                "market.cn_hk.quote_snapshot",
                {"market": market, "symbol": symbol},
                constraints={"market": market},
            ).data or {}
            fundamental = gateway.execute(
                "market.cn_hk.fundamentals",
                {"market": market, "symbol": symbol},
                constraints={"market": market},
            ).data or {}
            result: Dict[str, Any] = {
                "pe_ratio": None,
                "pb_ratio": None,
                "ps_ratio": None,
                "market_cap": None,
                "dividend_yield": None,
                "beta": None,
                "52w_high": None,
                "52w_low": None,
                "roe": None,
                "eps": None,
                "revenue_growth": None,
                "profit_margin": None,
                "debt_to_equity": None,
                "current_ratio": None,
                "free_cash_flow": None,
                "last": quote.get("last"),
                "previous_close": quote.get("previousClose"),
                "change_percent": quote.get("changePercent"),
                "source": fundamental.get("source"),
            }
            for key, value in fundamental.items():
                if key != "source" and value is not None:
                    result[key] = value
            return result
        except Exception as e:
            logger.debug(f"CN/HK fundamental failed: {market}:{symbol}: {e}")
            return None

    @staticmethod
    def _build_earnings_from_statements(stmts: Dict[str, Any]) -> Dict[str, Any]:
        """Construct an 'earnings' dict from structured financial_statements for CN/HK."""
        earnings: Dict[str, Any] = {}

        inc = stmts.get("income_statement") or {}
        latest_date = inc.get("latest_date")
        revenue = inc.get("total_revenue")
        net_income = inc.get("net_income")
        eps = inc.get("eps_diluted")

        if latest_date or revenue or net_income:
            earnings["quarterly"] = {
                "latest_quarter": latest_date,
                "revenue": revenue,
                "earnings": net_income,
            }
            earnings["history"] = [{
                "date": latest_date or "N/A",
                "eps_actual": eps,
                "eps_estimate": None,
                "surprise": None,
            }]

        cf = stmts.get("cash_flow") or {}
        bs = stmts.get("balance_sheet") or {}
        if cf or bs:
            summary_parts = []
            if cf.get("operating_cash_flow") is not None:
                summary_parts.append(f"Operating CF: {cf['operating_cash_flow']:,.0f}")
            if cf.get("free_cash_flow") is not None:
                summary_parts.append(f"FCF: {cf['free_cash_flow']:,.0f}")
            if bs.get("total_assets") is not None:
                summary_parts.append(f"Total Assets: {bs['total_assets']:,.0f}")
            if summary_parts:
                earnings["financial_summary"] = "; ".join(summary_parts)

        return earnings if earnings else {}

    def _get_us_fundamental(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch US fundamentals through the unified routing policy."""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        return dict(get_routed_external_data_gateway().execute(
            "analysis.us_fundamentals",
            {"market": "USStock", "symbol": symbol},
            constraints={"market": "USStock"},
        ).data or {})
    

    def _get_crypto_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """加密货币信息 (固定描述为主)"""
        crypto_info = {
            'BTC': {
                'name': 'Bitcoin',
                'description': '比特币，数字黄金，市值第一的加密货币，作为价值存储和避险资产',
                'category': 'Store of Value',
            },
            'ETH': {
                'name': 'Ethereum',
                'description': '以太坊，智能合约平台，DeFi和NFT生态的基础设施',
                'category': 'Smart Contract Platform',
            },
            'BNB': {
                'name': 'Binance Coin',
                'description': '币安币，全球最大交易所的平台代币',
                'category': 'Exchange Token',
            },
            'SOL': {
                'name': 'Solana',
                'description': '高性能公链，主打高TPS和低Gas费',
                'category': 'Smart Contract Platform',
            },
            'XRP': {
                'name': 'Ripple',
                'description': '瑞波币，专注跨境支付解决方案',
                'category': 'Payment',
            },
            'DOGE': {
                'name': 'Dogecoin',
                'description': '狗狗币，Meme币代表，社区驱动',
                'category': 'Meme',
            },
        }
        
        base = symbol.split('/')[0] if '/' in symbol else symbol
        base = base.upper()
        
        if base in crypto_info:
            return crypto_info[base]
        
        return {
            'name': base,
            'description': f'{base} 是一种加密货币',
            'category': 'Unknown',
        }

    def _get_crypto_factors(self, symbol: str, price_data: Dict[str, Any], kline_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """采集加密货币专属交易大数据因子。"""
        base_symbol = self._normalize_crypto_base_symbol(symbol)
        if not base_symbol:
            return {}

        market_structure = self._get_crypto_market_structure(base_symbol, price_data, kline_data)
        derivatives = self._get_crypto_derivatives_metrics(base_symbol)
        capital_flow = self._get_crypto_capital_flow(base_symbol)

        volume_24h = market_structure.get("volume_24h")
        volume_change_24h = market_structure.get("volume_change_24h")
        funding_rate = derivatives.get("funding_rate")
        oi_change = derivatives.get("open_interest_change_24h")
        long_short_ratio = derivatives.get("long_short_ratio")
        exchange_netflow = capital_flow.get("exchange_netflow")
        stablecoin_netflow = capital_flow.get("stablecoin_netflow")

        signals = {
            "derivatives_bias": self._derive_derivatives_bias(funding_rate, oi_change, long_short_ratio),
            "flow_bias": self._derive_flow_bias(exchange_netflow, stablecoin_netflow),
            "squeeze_risk": self._derive_squeeze_risk(funding_rate, long_short_ratio, oi_change),
            "volume_state": self._derive_volume_state(volume_change_24h),
        }

        summary = self._build_crypto_factor_summary(
            volume_change_24h=volume_change_24h,
            funding_rate=funding_rate,
            open_interest_change_24h=oi_change,
            exchange_netflow=exchange_netflow,
            stablecoin_netflow=stablecoin_netflow,
            signals=signals,
        )

        return {
            "symbol": base_symbol,
            "volume_24h": volume_24h,
            "volume_change_24h": volume_change_24h,
            "funding_rate": funding_rate,
            "open_interest": derivatives.get("open_interest"),
            "open_interest_change_24h": oi_change,
            "long_short_ratio": long_short_ratio,
            "exchange_netflow": exchange_netflow,
            "stablecoin_netflow": stablecoin_netflow,
            "signals": signals,
            "summary": summary,
            "sources": {
                "market_structure": market_structure.get("source"),
                "derivatives": derivatives.get("source"),
                "capital_flow": capital_flow.get("source"),
            }
        }

    def _normalize_crypto_base_symbol(self, symbol: str) -> str:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return ""
        if "/" in raw:
            raw = raw.split("/", 1)[0]
        if ":" in raw:
            raw = raw.split(":", 1)[0]
        raw = raw.replace("-USD", "").replace("-USDT", "")
        return raw

    def _cache_get(self, key: str) -> Optional[Any]:
        item = self._crypto_metric_cache.get(key)
        if not item:
            return None
        if float(item.get("expires_at") or 0) <= time.time():
            self._crypto_metric_cache.pop(key, None)
            return None
        return item.get("value")

    def _cache_set(self, key: str, value: Any, ttl_sec: int) -> Any:
        self._crypto_metric_cache[key] = {
            "value": value,
            "expires_at": time.time() + max(1, int(ttl_sec or 60)),
        }
        return value

    def _extract_latest_items(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("data", "result", "items", "list"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
                if isinstance(val, dict):
                    nested = self._extract_latest_items(val)
                    if nested:
                        return nested
        return []

    def _pick_latest_item(self, payload: Any) -> Dict[str, Any]:
        items = self._extract_latest_items(payload)
        if items:
            return items[-1]
        if isinstance(payload, dict):
            return payload
        return {}

    def _safe_num(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        if value is None or value == "":
            return default
        try:
            return float(str(value).replace(",", ""))
        except Exception:
            return default

    def _pick_number(self, payload: Any, *keys: str, default: Optional[float] = None) -> Optional[float]:
        if isinstance(payload, dict):
            for key in keys:
                if key in payload:
                    val = self._safe_num(payload.get(key), None)
                    if val is not None:
                        return val
            for val in payload.values():
                found = self._pick_number(val, *keys, default=None)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = self._pick_number(item, *keys, default=None)
                if found is not None:
                    return found
        return default

    def _get_crypto_market_structure(self, symbol: str, price_data: Dict[str, Any], kline_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        out = {
            "volume_24h": None,
            "volume_change_24h": None,
            "source": "price+kline",
        }
        try:
            quote_volume = self._safe_num(price_data.get("quoteVolume"))
            if quote_volume is not None:
                out["volume_24h"] = quote_volume
        except Exception:
            pass

        try:
            if len(kline_data) >= 2:
                latest_vol = self._safe_num(kline_data[-1].get("volume"), 0.0) or 0.0
                prev_vol = self._safe_num(kline_data[-2].get("volume"), 0.0) or 0.0
                if prev_vol > 0:
                    out["volume_change_24h"] = ((latest_vol - prev_vol) / prev_vol) * 100.0
        except Exception:
            pass

        if out["volume_24h"] is None or out["volume_change_24h"] is None:
            try:
                from app.services.data_routing.gateway import get_routed_external_data_gateway

                routed = dict(get_routed_external_data_gateway().execute(
                    "analysis.market_data_collection",
                    {"operation": "crypto_market_structure", "symbol": symbol},
                    constraints={"market": "Crypto"},
                ).data or {})
                if out["volume_24h"] is None:
                    out["volume_24h"] = self._safe_num(routed.get("volume_24h"))
                if out["volume_change_24h"] is None:
                    out["volume_change_24h"] = self._safe_num(routed.get("volume_change_24h"))
                out["source"] = routed.get("source") or out["source"]
            except Exception as exc:
                logger.debug("Routed crypto market structure unavailable for %s: %s", symbol, exc)
        return out

    def _get_crypto_derivatives_metrics(self, symbol: str) -> Dict[str, Any]:
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        return dict(get_routed_external_data_gateway().execute(
            "analysis.market_data_collection",
            {"operation": "crypto_derivatives", "symbol": symbol},
            constraints={"market": "Crypto"},
        ).data or {})

    def _get_crypto_capital_flow(self, symbol: str) -> Dict[str, Any]:
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        return dict(get_routed_external_data_gateway().execute(
            "analysis.market_data_collection",
            {"operation": "crypto_capital_flow", "symbol": symbol},
            constraints={"market": "Crypto"},
        ).data or {})

    def _derive_derivatives_bias(self, funding_rate: Optional[float], oi_change: Optional[float], long_short_ratio: Optional[float]) -> str:
        score = 0
        if funding_rate is not None:
            if funding_rate > 0:
                score += 1
            elif funding_rate < 0:
                score -= 1
        if oi_change is not None:
            if oi_change > 3:
                score += 1
            elif oi_change < -3:
                score -= 1
        if long_short_ratio is not None:
            if long_short_ratio > 1.2:
                score += 1
            elif long_short_ratio < 0.85:
                score -= 1
        if score >= 2:
            return "bullish"
        if score <= -2:
            return "bearish"
        return "neutral"

    def _derive_flow_bias(self, exchange_netflow: Optional[float], stablecoin_netflow: Optional[float]) -> str:
        score = 0
        if exchange_netflow is not None:
            if exchange_netflow < 0:
                score += 1
            elif exchange_netflow > 0:
                score -= 1
        if stablecoin_netflow is not None:
            if stablecoin_netflow > 0:
                score += 1
            elif stablecoin_netflow < 0:
                score -= 1
        if score >= 1:
            return "bullish"
        if score <= -1:
            return "bearish"
        return "neutral"

    def _derive_squeeze_risk(self, funding_rate: Optional[float], long_short_ratio: Optional[float], oi_change: Optional[float]) -> str:
        hot_long = (
            funding_rate is not None and funding_rate > 0.03 and
            long_short_ratio is not None and long_short_ratio > 1.5 and
            oi_change is not None and oi_change > 8
        )
        hot_short = (
            funding_rate is not None and funding_rate < -0.03 and
            long_short_ratio is not None and long_short_ratio < 0.75 and
            oi_change is not None and oi_change > 8
        )
        if hot_long or hot_short:
            return "high"
        if (
            (funding_rate is not None and abs(funding_rate) > 0.015) or
            (long_short_ratio is not None and (long_short_ratio > 1.3 or long_short_ratio < 0.85))
        ):
            return "medium"
        return "low"

    def _derive_volume_state(self, volume_change_24h: Optional[float]) -> str:
        if volume_change_24h is None:
            return "unknown"
        if volume_change_24h > 20:
            return "expanding"
        if volume_change_24h < -20:
            return "shrinking"
        return "stable"

    def _build_crypto_factor_summary(
        self,
        *,
        volume_change_24h: Optional[float],
        funding_rate: Optional[float],
        open_interest_change_24h: Optional[float],
        exchange_netflow: Optional[float],
        stablecoin_netflow: Optional[float],
        signals: Dict[str, Any],
    ) -> str:
        parts: List[str] = []
        if open_interest_change_24h is not None:
            parts.append(f"OI {'上升' if open_interest_change_24h >= 0 else '回落'} {abs(open_interest_change_24h):.1f}%")
        if funding_rate is not None:
            parts.append(f"资金费率{'偏正' if funding_rate >= 0 else '偏负'}")
        if exchange_netflow is not None:
            parts.append("交易所净流出" if exchange_netflow < 0 else "交易所净流入")
        if stablecoin_netflow is not None:
            parts.append("稳定币净流入增强" if stablecoin_netflow > 0 else "稳定币净流出")
        if volume_change_24h is not None:
            parts.append(f"成交活跃度{'放大' if volume_change_24h > 0 else '回落'}")
        direction = signals.get("derivatives_bias", "neutral")
        flow = signals.get("flow_bias", "neutral")
        squeeze = signals.get("squeeze_risk", "low")
        outlook = "偏多" if direction == "bullish" or flow == "bullish" else ("偏空" if direction == "bearish" or flow == "bearish" else "中性")
        risk_text = {"high": "拥挤风险高", "medium": "拥挤度抬升", "low": "拥挤风险低"}.get(squeeze, "风险未知")
        base = "、".join(parts[:4]) if parts else "链上与衍生品数据有限"
        return f"{base}，整体{outlook}，{risk_text}"
    
    def _get_company(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        """Derive company metadata from the routed fundamentals result."""
        fundamental = self._get_fundamental(market, symbol) or {}
        if not fundamental:
            return None
        keys = ("name", "full_name", "industry", "sector", "country", "exchange", "ipo_date", "market_cap", "website", "description")
        company = {key: fundamental.get(key) for key in keys if fundamental.get(key) is not None}
        return company or None

    def _get_macro_data(self, market: str, timeout: int = 10) -> Dict[str, Any]:
        """Fetch macro sentiment through the unified routing policy."""
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        payload = dict(get_routed_external_data_gateway().execute(
            "analysis.market_sentiment",
            {"operation": "aggregate", "market": market},
            constraints={"market": market or "GLOBAL"},
        ).data or {})
        mapping = {
            "vix": ("VIX", "VIX volatility index"),
            "dxy": ("DXY", "US dollar index"),
            "yield_curve": ("TNX", "US Treasury yield curve"),
            "fear_greed": ("FEAR_GREED", "Fear and Greed index"),
        }
        result = {}
        for source_key, (target_key, name) in mapping.items():
            item = payload.get(source_key)
            if not isinstance(item, dict):
                continue
            result[target_key] = {
                "name": name,
                "description": item.get("interpretation") or item.get("classification") or "",
                "price": item.get("value", item.get("yield_10y", 0)),
                "change": item.get("change", 0),
                "changePercent": item.get("change", 0),
                "level": item.get("level", "unknown"),
            }
        return result

    def _get_news(
        self, market: str, symbol: str, company_name: str = None, timeout: int = 8
    ) -> Dict[str, Any]:
        """Fetch news through the unified search routing policy."""
        from app.services.search import get_search_service

        response = get_search_service().search_stock_news(
            stock_code=symbol,
            stock_name=company_name or symbol,
            market=market,
            max_results=15,
        )
        rows = [
            {
                "datetime": item.published_date or "",
                "headline": item.title,
                "summary": (item.snippet or "")[:300],
                "source": item.source,
                "url": item.url,
                "sentiment": item.sentiment,
            }
            for item in response.results
        ]
        return {"news": rows, "sentiment": {}}


def get_market_data_collector() -> MarketDataCollector:
    """获取市场数据采集器单例"""
    global _collector
    if _collector is None:
        _collector = MarketDataCollector()
    return _collector
