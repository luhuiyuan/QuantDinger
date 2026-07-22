from datetime import date

import pandas as pd
import pytest

from app.services.cn_market_history.models import CoverageReport
from app.services.market.cn_stock_market import (
    CNStockDetailService,
    CNMarketSnapshotService,
    CNMarketSnapshotUnavailable,
    build_catalog_page,
    build_market_breadth,
    normalize_cn_snapshot_row,
)
from app.services.market.technical_indicators import calculate_indicator_package
from app.services.market import symbol_search
from app.data_sources.tencent import parse_quote_to_market_row


class _Cache:
    def __init__(self):
        self.values = {}
        self.set_calls = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ttl=0):
        self.values[key] = value
        self.set_calls.append(key)


def _row(code="600519", name="贵州茅台", latest=110.0, previous=100.0):
    return normalize_cn_snapshot_row(
        {
            "代码": code,
            "名称": name,
            "最新价": latest,
            "昨收": previous,
            "成交量": 12,
            "成交额": 345,
        },
        as_of="2026-07-21T01:00:00+00:00",
        source="test",
    )


def test_snapshot_row_normalizes_shenzhen_and_rejects_beijing():
    sz = _row(code="000001", name="平安银行", latest=10.5, previous=10)
    assert sz["instrument"] == "CNStock:000001.SZ"
    assert sz["changePercent"] == 5.0
    assert _row(code="830001") is None


def test_tencent_market_row_preserves_provider_quote_time():
    parts = [""] * 38
    parts[1], parts[2], parts[3], parts[4], parts[5] = "平安银行", "000001", "10.8", "10.7", "10.6"
    parts[6], parts[30], parts[33], parts[34], parts[37] = "100", "20260722150102", "11", "10", "123"
    row = parse_quote_to_market_row(parts)
    assert row["quoteTime"] == "2026-07-22T15:01:02+08:00"
    assert row["amount"] == 1230000


def test_symbol_lookup_accepts_canonical_cn_suffix(monkeypatch):
    monkeypatch.setattr(symbol_search, "seed_search_symbols", lambda **_kwargs: [{
        "market": "CNStock", "symbol": "600519", "name": "贵州茅台"
    }])
    found = symbol_search.find_market_symbol("CNStock", "600519.SH")
    assert found["symbol"] == "600519"


def test_snapshot_service_serves_stale_after_refresh_failure():
    cache = _Cache()
    calls = []

    def provider():
        calls.append(1)
        if len(calls) == 1:
            return {"rows": [_row()], "asOf": "t1", "source": "test"}
        raise RuntimeError("upstream down")

    service = CNMarketSnapshotService(provider=provider, cache=cache, fresh_ttl=1, stale_ttl=60)
    first = service.get_snapshot()
    cache.values.pop(service.fresh_key)
    second = service.get_snapshot()

    assert first["freshness"] == "fresh"
    assert second["freshness"] == "stale"
    assert second["warning"] == "upstream down"


def test_snapshot_service_raises_when_no_cache_exists():
    service = CNMarketSnapshotService(
        provider=lambda: (_ for _ in ()).throw(RuntimeError("down")),
        cache=_Cache(),
    )
    with pytest.raises(CNMarketSnapshotUnavailable):
        service.get_snapshot()


def test_market_breadth_uses_cn_price_limit_rules():
    rows = [
        _row("600519", "贵州茅台", 110, 100),
        _row("300001", "特锐德", 120, 100),
        _row("000001", "平安银行", 90, 100),
        _row("002001", "ST新股", 9.5, 10),
    ]
    out = build_market_breadth(rows, trade_date=date(2026, 7, 21))
    assert out["advancingCount"] == 2
    assert out["decliningCount"] == 2
    assert out["limitUpCount"] == 2
    assert out["limitDownCount"] == 2


def test_catalog_page_filters_before_pagination_and_loads_page_watchlist_only():
    catalog = [
        {"instrument": "CNStock:600519.SH", "code": "600519", "symbol": "600519.SH", "exchange": "SH", "name": "贵州茅台"},
        {"instrument": "CNStock:000001.SZ", "code": "000001", "symbol": "000001.SZ", "exchange": "SZ", "name": "平安银行"},
        {"instrument": "CNStock:300001.SZ", "code": "300001", "symbol": "300001.SZ", "exchange": "SZ", "name": "特锐德"},
    ]
    snapshot = {
        "rows": [_row("600519", latest=101), _row("000001", latest=99), _row("300001", latest=102)],
        "asOf": "t1", "source": "test", "freshness": "fresh", "status": "available",
    }
    requested = []
    result = build_catalog_page(
        catalog,
        snapshot,
        page=1,
        page_size=1,
        change_state="up",
        watchlist_loader=lambda symbols: requested.extend(symbols) or {"600519.SH"},
    )
    assert result["pagination"]["total"] == 2
    assert requested == ["600519.SH"]
    assert result["items"][0]["watchlisted"] is True


def _identity():
    return {
        "instrument": "CNStock:600519.SH", "code": "600519", "symbol": "600519.SH",
        "exchange": "SH", "name": "贵州茅台",
    }


def _daily_bars(count=40):
    output = []
    for index in range(count):
        close = 100 + index * 0.5
        output.append({
            "date": f"2026-06-{(index % 28) + 1:02d}",
            "time": 1_700_000_000 + index * 86400,
            "open": close - 0.2, "high": close + 1, "low": close - 1,
            "close": close, "volume": 1000 + index, "amount": 100000 + index,
        })
    return output


def test_indicator_package_includes_kdj_and_explicit_availability():
    package = calculate_indicator_package(_daily_bars(40), data_version="v1")
    assert package["values"]["kdj"]["signal"] in {"bullish", "bearish", "overbought", "oversold", "neutral"}
    assert package["availability"]["macd"]["available"] is True
    short = calculate_indicator_package(_daily_bars(8), data_version="v2")
    assert short["availability"]["kdj"] == {
        "available": False, "requiredBars": 9, "actualBars": 8, "reason": "insufficient_history"
    }


def test_detail_history_prefers_complete_local_history(monkeypatch):
    index = pd.date_range("2026-06-01", periods=40, freq="D")
    frame = pd.DataFrame({
        "open": [100.0] * 40, "high": [102.0] * 40, "low": [99.0] * 40,
        "close": [101.0] * 40, "volume": [1000.0] * 40, "amount": [100000.0] * 40,
    }, index=index)

    class _Query:
        def load(self, *_args, **_kwargs):
            return type("Result", (), {"frame": frame, "provenance": {"dataVersion": "local-v1", "provider": "easy_tdx"}})()

    class _Quality:
        def assess(self, instrument, start, end, persist=False):
            report = CoverageReport(instrument, start, end, start, end, 40, 40, complete=True, data_version="local-v1")
            return type("Assessment", (), {"report": report})()

    class _Kline:
        def get_kline(self, **_kwargs):
            raise AssertionError("fallback must not run")

    monkeypatch.setattr("app.services.market.cn_stock_market.latest_completed_session", lambda _market: pd.Timestamp("2026-07-20"))
    service = CNStockDetailService(query_service=_Query(), quality_service=_Quality(), kline_service=_Kline(), cache=_Cache())
    result = service.history(_identity(), limit=40, adjustment="raw")
    assert result["provenance"]["tier"] == "local_authoritative"
    assert result["provenance"]["backtestEligible"] is True
    assert len(result["bars"]) == 40


def test_detail_history_marks_fallback_as_not_backtest_eligible(monkeypatch):
    class _Query:
        def load(self, *_args, **_kwargs):
            raise RuntimeError("local missing")

    class _Kline:
        def get_kline(self, **_kwargs):
            return _daily_bars(40)

    monkeypatch.setattr("app.services.market.cn_stock_market.latest_completed_session", lambda _market: pd.Timestamp("2026-07-20"))
    service = CNStockDetailService(query_service=_Query(), kline_service=_Kline(), cache=_Cache())
    result = service.history(_identity(), limit=40, adjustment="forward")
    assert result["provenance"]["tier"] == "display_fallback"
    assert result["provenance"]["backtestEligible"] is False
    assert result["indicators"]["dataVersion"] == result["provenance"]["dataVersion"]


def test_indicator_cache_key_changes_with_history_version(monkeypatch):
    class _Query:
        def load(self, *_args, **_kwargs):
            raise RuntimeError("local missing")

    class _Kline:
        calls = 0

        def get_kline(self, **_kwargs):
            self.calls += 1
            rows = _daily_bars(40)
            rows[-1]["close"] += self.calls
            return rows

    monkeypatch.setattr("app.services.market.cn_stock_market.latest_completed_session", lambda _market: pd.Timestamp("2026-07-20"))
    cache = _Cache()
    service = CNStockDetailService(query_service=_Query(), kline_service=_Kline(), cache=cache)
    first = service.history(_identity(), limit=40, adjustment="forward")
    second = service.history(_identity(), limit=40, adjustment="forward")
    assert first["provenance"]["dataVersion"] != second["provenance"]["dataVersion"]
    indicator_keys = [key for key in cache.set_calls if "cn_market:indicators" in key]
    assert len(set(indicator_keys)) == 2
