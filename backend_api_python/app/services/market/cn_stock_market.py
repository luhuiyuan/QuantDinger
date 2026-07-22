"""沪深 A 股市场快照、概览和目录查询服务。"""

from __future__ import annotations

import math
import os
import threading
import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from app.data_sources.tencent import (
    fetch_quote,
    fetch_quote_map,
    normalize_cn_code,
    parse_quote_to_market_row,
    parse_quote_to_ticker,
)
from app.services.cn_market_history.instruments import CNInstrumentError, parse_cn_instrument
from app.services.cn_market_history.models import AdjustmentMode
from app.services.cn_market_history.query_service import (
    AdjustmentCoverageError,
    CNMarketHistoryQueryService,
)
from app.services.cn_market_history.quality import CNMarketHistoryQualityService
from app.services.kline import KlineService
from app.services.market.technical_indicators import calculate_indicator_package
from app.services.market_schedule import latest_completed_session
from app.services.strategy_v2.execution_policy import CNStockExecutionPolicy
from app.utils.cache import CacheManager
from app.utils.db import get_db_connection


INDEX_DEFINITIONS = (
    ("000001.SH", "上证指数", "sh000001"),
    ("399001.SZ", "深证成指", "sz399001"),
    ("399006.SZ", "创业板指", "sz399006"),
)

_snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cn-market-snapshot")


class CNMarketSnapshotUnavailable(RuntimeError):
    code = "cn_market.snapshot_unavailable"


def _number(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() in {"", "-", "--", "None", "nan"}:
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _text(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_cn_snapshot_row(row: dict, *, as_of: str, source: str) -> dict | None:
    """将上游全市场行规范化，仅接受严格沪深普通 A 股代码。"""
    code = _text(row, "code", "symbol", "代码").upper()
    try:
        instrument = parse_cn_instrument(code)
    except CNInstrumentError:
        return None
    name = _text(row, "name", "名称") or code
    latest = _number(row.get("latest", row.get("最新价")))
    previous_close = _number(row.get("previousClose", row.get("昨收")))
    change = _number(row.get("change", row.get("涨跌额")))
    change_pct = _number(row.get("changePercent", row.get("涨跌幅")))
    if change is None and latest is not None and previous_close:
        change = latest - previous_close
    if change_pct is None and change is not None and previous_close:
        change_pct = change / previous_close * 100
    return {
        "instrument": instrument.canonical,
        "code": instrument.code,
        "symbol": f"{instrument.code}.{instrument.exchange}",
        "exchange": instrument.exchange,
        "name": name,
        "latest": latest,
        "change": round(change, 4) if change is not None else None,
        "changePercent": round(change_pct, 4) if change_pct is not None else None,
        "open": _number(row.get("open", row.get("今开"))),
        "high": _number(row.get("high", row.get("最高"))),
        "low": _number(row.get("low", row.get("最低"))),
        "previousClose": previous_close,
        "volume": _number(row.get("volume", row.get("成交量"))),
        "amount": _number(row.get("amount", row.get("成交额"))),
        "asOf": as_of,
        "source": source,
    }


def fetch_cn_market_snapshot() -> dict:
    """通过现有 AkShare 依赖一次获取沪深 A 股展示快照。"""
    import akshare as ak  # type: ignore

    fetched_at = datetime.now(timezone.utc).isoformat()
    frame = ak.stock_zh_a_spot_em()
    records = frame.to_dict("records") if frame is not None else []
    source = "eastmoney-akshare"
    rows = [
        normalized
        for row in records
        if (normalized := normalize_cn_snapshot_row(row, as_of=fetched_at, source=source))
    ]
    if not rows:
        raise CNMarketSnapshotUnavailable("A-share snapshot returned no Shanghai/Shenzhen rows")
    return {"rows": rows, "asOf": fetched_at, "source": source}


def fetch_cn_quote_rows(symbols: Iterable[str]) -> list[dict]:
    """Fetch a page-sized batch of Tencent quotes for degraded list display."""
    requested = list(symbols or [])[:100]
    codes = [normalize_cn_code(symbol) for symbol in requested]
    by_code = fetch_quote_map(codes)
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for code in codes:
        raw = by_code.get(code.lower())
        if not raw:
            continue
        normalized = normalize_cn_snapshot_row(
            parse_quote_to_market_row(raw), as_of=fetched_at, source="tencent-batch"
        )
        if normalized:
            rows.append(normalized)
    return rows


class CNMarketSnapshotService:
    """共享新鲜/陈旧快照；同一进程内合并并发刷新。"""

    def __init__(
        self,
        provider: Callable[[], dict] = fetch_cn_market_snapshot,
        cache: CacheManager | None = None,
        *,
        fresh_ttl: int | None = None,
        stale_ttl: int | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache or CacheManager()
        self.fresh_ttl = int(fresh_ttl or os.getenv("CN_MARKET_SNAPSHOT_TTL_SEC", "30"))
        self.stale_ttl = int(stale_ttl or os.getenv("CN_MARKET_SNAPSHOT_STALE_TTL_SEC", "900"))
        self.fresh_key = "cn_market:snapshot:fresh:v1"
        self.stale_key = "cn_market:snapshot:stale:v1"
        self.timeout_sec = float(os.getenv("CN_MARKET_SNAPSHOT_TIMEOUT_SEC", "12"))
        self._refresh_lock = threading.Lock()
        self._refresh_future: Future | None = None

    @staticmethod
    def _decorate(payload: dict, freshness: str, warning: str = "") -> dict:
        return {
            **payload,
            "freshness": freshness,
            "status": "available" if freshness == "fresh" else "degraded",
            "warning": warning or None,
        }

    def get_snapshot(self, *, force: bool = False) -> dict:
        if not force:
            fresh = self.cache.get(self.fresh_key)
            if fresh is not None:
                return self._decorate(fresh, "fresh")

        with self._refresh_lock:
            if not force:
                fresh = self.cache.get(self.fresh_key)
                if fresh is not None:
                    return self._decorate(fresh, "fresh")
            if self._refresh_future is None or self._refresh_future.done():
                self._refresh_future = _snapshot_executor.submit(self.provider)
            future = self._refresh_future
        try:
            payload = future.result(timeout=max(0.1, self.timeout_sec))
            if not payload.get("rows"):
                raise CNMarketSnapshotUnavailable("A-share snapshot is empty")
            self.cache.set(self.fresh_key, payload, self.fresh_ttl)
            self.cache.set(self.stale_key, payload, self.stale_ttl)
            return self._decorate(payload, "fresh")
        except FuturesTimeoutError as exc:
            failure: Exception = CNMarketSnapshotUnavailable("A-share snapshot refresh timed out")
            failure.__cause__ = exc
            # A running thread cannot be forcefully killed. Detach it so a
            # later request can start a fresh attempt instead of waiting on
            # the same blocked upstream call forever.
            with self._refresh_lock:
                if self._refresh_future is future:
                    future.cancel()
                    self._refresh_future = None
        except Exception as exc:
            failure = exc
        stale = self.cache.get(self.stale_key)
        if stale is not None:
            return self._decorate(stale, "stale", str(failure))
        raise CNMarketSnapshotUnavailable(str(failure)) from failure


def fetch_core_indices() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    quote_codes = [provider_code for _symbol, _name, provider_code in INDEX_DEFINITIONS]
    try:
        quote_map = fetch_quote_map(quote_codes)
    except Exception:
        quote_map = {}
    output = []
    for symbol, default_name, provider_code in INDEX_DEFINITIONS:
        try:
            parts = quote_map.get(provider_code.lower()) or fetch_quote(provider_code)
            ticker = parse_quote_to_ticker(parts or []) if parts else {}
            latest = _number(ticker.get("last"))
            if latest is None or latest <= 0:
                raise ValueError("quote unavailable")
            output.append({
                "symbol": symbol,
                "name": ticker.get("name") or default_name,
                "latest": latest,
                "change": _number(ticker.get("change")),
                "changePercent": _number(ticker.get("changePercent")),
                "asOf": now,
                "source": "tencent",
                "freshness": "fresh",
                "status": "available",
            })
        except Exception as exc:
            output.append({
                "symbol": symbol,
                "name": default_name,
                "latest": None,
                "change": None,
                "changePercent": None,
                "asOf": None,
                "source": "tencent",
                "freshness": "unavailable",
                "status": "unavailable",
                "warning": str(exc),
            })
    return output


def _classification(row: dict) -> tuple[str, str] | None:
    code = str(row.get("code") or "")
    name = str(row.get("name") or "").upper()
    if code.startswith("688"):
        board = "star_board"
    elif code.startswith(("300", "301")):
        board = "chinext"
    elif code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        board = "main_board"
    else:
        return None
    status = "st" if "ST" in name else "non_st"
    return board, status


def build_market_breadth(rows: Iterable[dict], *, trade_date: date | None = None) -> dict:
    session_date = trade_date or date.today()
    stats = {
        "coveredCount": 0,
        "advancingCount": 0,
        "decliningCount": 0,
        "flatCount": 0,
        "limitUpCount": 0,
        "limitDownCount": 0,
        "unclassifiedLimitCount": 0,
        "totalAmount": 0.0,
    }
    for row in rows:
        latest = _number(row.get("latest"))
        previous = _number(row.get("previousClose"))
        amount = _number(row.get("amount"))
        if amount is not None and amount >= 0:
            stats["totalAmount"] += amount
        if latest is None or previous is None or previous <= 0:
            continue
        stats["coveredCount"] += 1
        delta = latest - previous
        if delta > 0.000001:
            stats["advancingCount"] += 1
        elif delta < -0.000001:
            stats["decliningCount"] += 1
        else:
            stats["flatCount"] += 1

        classification = _classification(row)
        if classification is None:
            stats["unclassifiedLimitCount"] += 1
            continue
        board, status = classification
        rate = CNStockExecutionPolicy._price_limit_rate(
            str(row.get("symbol") or ""),
            {
                "classification_confirmed": True,
                "board_classification": board,
                "status_classification": status,
            },
            session_date,
        )
        if rate is None:
            stats["unclassifiedLimitCount"] += 1
            continue
        upper = round(previous * (1 + rate) + 1e-9, 2)
        lower = round(previous * (1 - rate) + 1e-9, 2)
        if latest >= upper - 0.0050001:
            stats["limitUpCount"] += 1
        if latest <= lower + 0.0050001:
            stats["limitDownCount"] += 1
    stats["totalAmount"] = round(stats["totalAmount"], 2)
    return stats


def load_cn_symbol_catalog(keyword: str = "", exchange: str = "") -> list[dict]:
    keyword = str(keyword or "").strip()
    exchange = str(exchange or "").strip().upper()
    pattern = f"%{keyword}%"
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol, name
            FROM qd_market_symbols
            WHERE market = 'CNStock' AND is_active = 1
              AND (? = '' OR symbol ILIKE ? OR name ILIKE ?)
            ORDER BY symbol ASC
            """,
            (keyword, pattern, pattern),
        )
        rows = cur.fetchall() or []
        cur.close()
    output = []
    for row in rows:
        try:
            instrument = parse_cn_instrument(row.get("symbol"))
        except CNInstrumentError:
            continue
        if exchange and instrument.exchange != exchange:
            continue
        output.append({
            "instrument": instrument.canonical,
            "code": instrument.code,
            "symbol": f"{instrument.code}.{instrument.exchange}",
            "exchange": instrument.exchange,
            "name": row.get("name") or instrument.code,
        })
    return output


def load_cn_symbol(symbol: str) -> dict | None:
    try:
        instrument = parse_cn_instrument(symbol)
    except CNInstrumentError:
        return None
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol, name FROM qd_market_symbols
            WHERE market = 'CNStock' AND is_active = 1 AND symbol = ?
            ORDER BY id ASC LIMIT 1
            """,
            (instrument.code,),
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {
        "instrument": instrument.canonical,
        "code": instrument.code,
        "symbol": f"{instrument.code}.{instrument.exchange}",
        "exchange": instrument.exchange,
        "name": row.get("name") or instrument.code,
    }


def load_cn_watchlist_symbols(user_id: int, symbols: list[str]) -> set[str]:
    if not symbols:
        return set()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol FROM qd_watchlist
            WHERE user_id = ? AND market = 'CNStock' AND symbol = ANY(?)
            """,
            (user_id, symbols),
        )
        rows = cur.fetchall() or []
        cur.close()
    return {str(row.get("symbol") or "").upper() for row in rows}


def _change_matches(row: dict, state: str) -> bool:
    value = _number(row.get("changePercent"))
    if value is None:
        return False
    if state == "up":
        return value > 0
    if state == "down":
        return value < 0
    if state == "flat":
        return abs(value) <= 0.000001
    return True


def build_catalog_page(
    catalog: list[dict],
    snapshot: dict,
    *,
    page: int,
    page_size: int,
    change_state: str = "",
    watchlist_loader: Callable[[list[str]], set[str]] | None = None,
) -> dict:
    quotes = {row["instrument"]: row for row in snapshot.get("rows") or []}
    merged = []
    for item in catalog:
        quote = quotes.get(item["instrument"])
        row = {
            **item,
            **(quote or {}),
            "quoteStatus": "available" if quote and quote.get("latest") is not None else "unavailable",
        }
        if change_state and not _change_matches(row, change_state):
            continue
        merged.append(row)
    total = len(merged)
    start = (page - 1) * page_size
    items = merged[start:start + page_size]
    watched = watchlist_loader([item["symbol"] for item in items]) if watchlist_loader else set()
    for item in items:
        item["watchlisted"] = item["symbol"] in watched or item["code"] in watched
    return {
        "items": items,
        "pagination": {"page": page, "pageSize": page_size, "total": total},
        "snapshot": {
            "asOf": snapshot.get("asOf"),
            "source": snapshot.get("source"),
            "freshness": snapshot.get("freshness"),
            "status": snapshot.get("status"),
            "warning": snapshot.get("warning"),
        },
    }


def _frame_to_bars(frame) -> list[dict]:
    output = []
    for timestamp, row in frame.iterrows():
        output.append({
            "date": timestamp.date().isoformat(),
            "time": int(timestamp.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume") or 0),
            "amount": float(row.get("amount") or 0),
        })
    return output


def _fallback_version(bars: list[dict]) -> str:
    body = json.dumps(bars, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest() if bars else ""


class CNStockDetailService:
    def __init__(
        self,
        *,
        snapshot_service: CNMarketSnapshotService | None = None,
        query_service: CNMarketHistoryQueryService | None = None,
        quality_service: CNMarketHistoryQualityService | None = None,
        kline_service: KlineService | None = None,
        cache: CacheManager | None = None,
    ) -> None:
        self.snapshot_service = snapshot_service or CNMarketSnapshotService()
        self.query_service = query_service or CNMarketHistoryQueryService()
        self.quality_service = quality_service or CNMarketHistoryQualityService()
        self.kline_service = kline_service or KlineService()
        self.cache = cache or CacheManager()

    def detail(self, identity: dict, *, watchlisted: bool = False) -> dict:
        try:
            snapshot = self.snapshot_service.get_snapshot()
        except CNMarketSnapshotUnavailable as exc:
            try:
                fallback_rows = fetch_cn_quote_rows([identity["symbol"]])
            except Exception as fallback_error:
                raise CNMarketSnapshotUnavailable(
                    f"A-share snapshot unavailable; Tencent quote fallback failed: {fallback_error}"
                ) from fallback_error
            snapshot = {
                "rows": fallback_rows,
                "asOf": fallback_rows[0].get("asOf") if fallback_rows else None,
                "source": "tencent-batch" if fallback_rows else "unavailable",
                "freshness": "fresh" if fallback_rows else "unavailable",
                "status": "available" if fallback_rows else "unavailable",
                "warning": str(exc),
            }
        quote = next(
            (row for row in snapshot.get("rows") or [] if row.get("instrument") == identity["instrument"]),
            None,
        )
        return {
            **identity,
            "quote": quote,
            "quoteStatus": "available" if quote and quote.get("latest") is not None else "unavailable",
            "watchlisted": bool(watchlisted),
            "snapshot": {
                "asOf": snapshot.get("asOf"),
                "source": snapshot.get("source"),
                "freshness": snapshot.get("freshness"),
                "warning": snapshot.get("warning"),
            },
        }

    def history(self, identity: dict, *, limit: int = 260, adjustment: str = "forward") -> dict:
        mode = AdjustmentMode(adjustment)
        end_date = latest_completed_session("CNStock").date()
        start_date = end_date - timedelta(days=max(420, int(limit * 1.8)))
        bars: list[dict] = []
        provenance: dict = {}
        try:
            result = self.query_service.load(
                identity["instrument"], start_date, end_date, mode=mode
            )
            if result.frame.empty:
                raise ValueError("local history is empty")
            first_date = result.frame.index[0].date()
            quality = self.quality_service.assess(
                identity["instrument"], first_date, end_date, persist=False
            )
            if not quality.report.complete:
                raise ValueError("local history coverage incomplete")
            bars = _frame_to_bars(result.frame.tail(limit))
            provenance = {
                **result.provenance,
                "tier": "local_authoritative",
                "backtestEligible": True,
                "freshness": "closed_daily",
            }
        except Exception as local_error:
            fallback = self.kline_service.get_kline(
                market="CNStock",
                symbol=identity["symbol"],
                timeframe="1D",
                limit=limit,
            )
            bars = []
            for row in fallback or []:
                item = dict(row)
                timestamp = item.get("time")
                if timestamp and not item.get("date"):
                    item["date"] = datetime.fromtimestamp(int(timestamp)).date().isoformat()
                bars.append(item)
            version = _fallback_version(bars)
            provenance = {
                "instrument": identity["instrument"],
                "provider": "cnstock-display-chain",
                "providerVersion": None,
                "dataVersion": version,
                "adjustmentMode": adjustment,
                "factorVersion": None,
                "firstTradeDate": bars[0].get("date") if bars else None,
                "lastTradeDate": bars[-1].get("date") if bars else None,
                "tier": "display_fallback" if bars else "unavailable",
                "backtestEligible": False,
                "freshness": "closed_daily" if bars else "unavailable",
                "warning": str(local_error),
            }
        version = str(provenance.get("dataVersion") or _fallback_version(bars))
        indicator_key = f"cn_market:indicators:v1:{identity['instrument']}:{adjustment}:{version}"
        indicators = self.cache.get(indicator_key)
        if indicators is None:
            indicators = calculate_indicator_package(bars, data_version=version)
            self.cache.set(indicator_key, indicators, ttl=3600)
        return {"bars": bars, "indicators": indicators, "provenance": provenance}
