from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.data_routing.adapters import production_transports as transports
from app.services.data_routing.adapters.catalog import _ADAPTER_CAPABILITIES
from app.services.data_routing.default_bootstrap import DEFAULT_ROUTING_PREFERENCES


def _deadline():
    return datetime.now(timezone.utc) + timedelta(seconds=20)


def test_catalog_registers_all_cn_snapshot_channels():
    for adapter_key in ("akshare", "eastmoney", "sina", "tencent", "easy_tdx"):
        assert "cn_market_snapshot" in _ADAPTER_CAPABILITIES[adapter_key]


def test_a_share_provider_capability_matrix_is_complete_for_product_features():
    expected = {
        "eastmoney": {"cn_fundamental_history", "cn_market_snapshot"},
        "akshare": {"asia_equity_kline", "cn_hk_fundamentals", "cn_hk_quote", "cn_market_snapshot", "market_catalog", "symbol_master"},
        "tencent": {"asia_equity_kline", "cn_hk_quote", "cn_market_snapshot", "symbol_reference"},
        "easy_tdx": {"asia_equity_kline", "cn_equity_history", "cn_corporate_actions", "cn_hk_quote", "cn_market_snapshot"},
        "eastmoney": {"cn_fundamental_history", "cn_hk_quote", "cn_market_snapshot"},
        "cninfo": {"cn_corporate_announcement", "cn_official_adjustment_reference"},
        "cn_exchange_official": {"cn_official_adjustment_reference"},
        "sina": {"asia_equity_kline", "cn_equity_history", "cn_hk_fundamentals", "cn_hk_quote", "cn_market_snapshot", "market_catalog", "symbol_master", "symbol_reference"},
    }
    for adapter_key, capabilities in expected.items():
        assert capabilities.issubset(_ADAPTER_CAPABILITIES[adapter_key])

    assert DEFAULT_ROUTING_PREFERENCES["cn_market_snapshot"] == (
        "eastmoney", "akshare", "sina", "tencent", "easy_tdx"
    )


def test_sina_daily_kline_maps_a_share_bars(monkeypatch):
    class Frame:
        def to_dict(self, orient):
            assert orient == "records"
            return [{"date": "2026-08-03", "open": 9, "high": 11, "low": 8, "close": 10, "volume": 2, "amount": 3}]
    monkeypatch.setattr("akshare.stock_zh_a_daily", lambda **kwargs: Frame())

    rows = transports._sina("asia_equity_kline", {"symbol": "600000", "market": "CNStock", "limit": 1}, {"timeframe": "1D"}, {}, {}, _deadline())

    assert rows[0]["close"] == 10.0 and rows[0]["volume"] == 2.0


def test_sina_history_maps_daily_page(monkeypatch):
    monkeypatch.setattr(transports, "_sina_daily_records", lambda *_args, **_kwargs: [{"trade_date": datetime(2026, 8, 3).date(), "open": 9, "high": 11, "low": 8, "close": 10, "volume": 2, "amount": 3}])

    page = transports._sina("cn_equity_history", {"operation": "daily_page", "instrument": "SH:600000", "start_date": "2026-08-01", "end_date": "2026-08-04"}, {}, {}, {}, _deadline())

    assert page.reached_start and page.bars[0].provider == "sina"


def test_snapshot_symbols_uses_integer_active_flag(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, query, args):
            executed.append((query, args))

        def fetchall(self):
            return [{"symbol": "600000"}]

        def close(self):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    monkeypatch.setattr("app.utils.db.get_db_connection", lambda: Connection())

    assert transports._snapshot_symbols({}, {}) == ["600000"]
    assert "is_active=1" in executed[0][0]


def test_tencent_snapshot_uses_bounded_symbol_batches(monkeypatch):
    monkeypatch.setattr(transports, "_snapshot_symbols", lambda *_args: ["600000", "000001", "300001"])
    monkeypatch.setattr(transports, "_snapshot_timeout", lambda *_args: 5)

    calls = []

    def quote_map(codes, timeout):
        calls.append((codes, timeout))
        return {
            code.lower(): ["", "测试", code[2:], "10", "9", "9.5", "100", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "20260804100000", "", "", "11", "8", "", "", "2"]
            for code in codes
        }

    monkeypatch.setattr("app.data_sources.tencent.fetch_quote_map", quote_map)
    result = transports._tencent("cn_market_snapshot", {"market": "CNStock", "subject": "all"}, {}, {"snapshot_batch_size": 2}, {}, _deadline())

    assert [len(codes) for codes, _timeout in calls] == [2, 1]
    assert result["source"] == "tencent-batch"
    assert len(result["rows"]) == 3


def test_eastmoney_snapshot_maps_provider_fields(monkeypatch):
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": {"diff": [{"f12": "600000", "f14": "浦发银行", "f2": 10, "f3": 1, "f4": 0.1, "f5": 2, "f6": 3, "f15": 11, "f16": 9, "f17": 9.5, "f18": 9.9}]}},
    )
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: response)

    result = transports._eastmoney("cn_market_snapshot", {"market": "CNStock", "subject": "all"}, {}, {}, {}, _deadline())

    assert result["source"] == "eastmoney"
    assert result["rows"][0]["code"] == "600000"
    assert result["rows"][0]["latest"] == 10.0


def test_sina_snapshot_maps_bounded_quote_rows(monkeypatch):
    monkeypatch.setattr(transports, "_snapshot_symbols", lambda *_args: ["600000"])
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        content='var hq_str_sh600000="浦发银行,9.5,9.9,10,11,9,0,0,2,3";'.encode("gbk"),
    )
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: response)

    result = transports._sina_snapshot({"market": "CNStock", "subject": "all"}, {}, _deadline())

    assert result["source"] == "sina"
    assert result["rows"][0]["code"] == "600000"
    assert result["rows"][0]["amount"] == 3.0


def test_sina_adapter_routes_snapshot_capability(monkeypatch):
    sentinel = {"source": "sina", "rows": [{"code": "600000"}]}
    monkeypatch.setattr(transports, "_sina_snapshot", lambda subject, config, deadline: sentinel)

    result = transports._sina("cn_market_snapshot", {"market": "CNStock"}, {}, {}, {}, _deadline())

    assert result is sentinel


def test_easy_tdx_snapshot_uses_quote_batches(monkeypatch):
    monkeypatch.setattr(transports, "_snapshot_symbols", lambda *_args: ["600000", "000001"])

    class Provider:
        def probe_hosts(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def fetch_market_quotes(self, instruments):
            assert len(instruments) == 1
            return [{"code": instruments[0].code, "name": "测试", "price": 10, "last_close": 9, "open": 9.5, "high": 11, "low": 8, "vol": 2, "amount": 3}]

    monkeypatch.setattr("app.services.cn_market_history.tdx_provider.TDXProvider", Provider)
    result = transports._easy_tdx("cn_market_snapshot", {"market": "CNStock", "subject": "all"}, {}, {"snapshot_batch_size": 1}, {}, _deadline())

    assert result["source"] == "easy_tdx"
    assert len(result["rows"]) == 2
