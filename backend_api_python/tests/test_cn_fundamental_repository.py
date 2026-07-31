from datetime import date

from app.services.cn_fundamental_history.repository import CNFundamentalHistoryRepository, _hash


def test_pit_query_keeps_only_available_versions_and_merges_official_fields():
    repository = CNFundamentalHistoryRepository()
    captured = {}
    rows = [
        {"period_end": date(2025, 12, 31), "available_at": date(2026, 4, 1), "source": "eastmoney", "source_version": "a", "observation_version": 2, "announcement_ref": {}, "field_code": "revenue", "value_numeric": 100, "value_text": None},
        {"period_end": date(2025, 12, 31), "available_at": date(2026, 4, 2), "source": "cninfo", "source_version": "b", "observation_version": 1, "announcement_ref": {"id": "x"}, "field_code": "parent_net_income", "value_numeric": 20, "value_text": None},
    ]

    def fetch(sql, params):
        captured["sql"], captured["params"] = sql, params
        return rows

    repository._fetchall = fetch
    result = repository.load_as_of("CNStock:600519.SH", date(2026, 4, 30))
    assert "available_at<=%s" in captured["sql"]
    assert captured["params"][1] == date(2026, 4, 30)
    assert result[0]["revenue"] == 100
    assert result[0]["parent_net_income"] == 20
    assert [item["source"] for item in result[0]["sources"]] == ["eastmoney", "cninfo"]


def test_eligible_universe_includes_delisted_but_only_shenzhen_shanghai_ordinary_shares():
    repository = CNFundamentalHistoryRepository()
    captured = {}
    repository._fetchall = lambda sql, params: captured.setdefault("call", (sql, params)) and [
        {"symbol": "600519"}, {"symbol": "000001.SZ"}, {"symbol": "not-a-share"},
    ]
    assert repository.list_eligible_instruments() == ["CNStock:600519.SH", "CNStock:000001.SZ"]
    assert "delisted_on" not in captured["call"][0]
    assert "security_type='ordinary_share'" in captured["call"][0]
    assert "qd_market_symbols" in captured["call"][0]


def test_provider_revision_has_distinct_content_hash_and_is_not_an_overwrite():
    assert _hash({"revenue": 100, "revision": 1}) != _hash({"revenue": 101, "revision": 2})
