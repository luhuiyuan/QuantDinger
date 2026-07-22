from datetime import datetime, timezone

import pytest

from app.services.market import cn_stock_quote_snapshots as module
from app.services.market.cn_stock_quote_snapshots import (
    CNStockQuoteRefreshService,
    rebuild_cn_market_overview_cache,
    query_cn_stock_snapshot_page,
    quote_refresh_decision,
)


class _Repo:
    def __init__(self):
        self.runs = []
        self.finished = []
        self.rows = []

    def create_run(self, trigger_kind, status="running", reason=""):
        self.runs.append((trigger_kind, status, reason))
        return f"run-{len(self.runs)}"

    def finish_run(self, run_id, **payload):
        self.finished.append((run_id, payload))

    def final_refresh_completed(self, _date):
        return False

    def upsert_quotes(self, rows, run_id):
        rows = list(rows)
        self.rows.extend((run_id, row) for row in rows)
        return len(rows)


class _Cache:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = []
        self.values = {}

    def acquire_lock(self, *_args):
        return self.acquired

    def release_lock(self, key, owner):
        self.released.append((key, owner))
        return True

    def delete(self, _key):
        pass

    def set(self, key, value, ttl=0):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


def _catalog():
    return [
        {"symbol": "000001.SZ"},
        {"symbol": "600519.SH"},
    ]


def _quote(symbol):
    code, exchange = symbol.split(".")
    return {
        "instrument": f"CNStock:{symbol}", "symbol": symbol, "code": code,
        "exchange": exchange, "latest": 10, "previousClose": 9,
        "asOf": "2026-07-22T02:00:00+00:00", "source": "test",
    }


def test_refresh_decision_covers_session_break_and_final_window(monkeypatch):
    class _Calendar:
        def is_session(self, _value):
            return True

    monkeypatch.setattr(module, "_calendar", lambda _market: _Calendar())
    assert quote_refresh_decision(datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)).run is True
    assert quote_refresh_decision(datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc)).reason == "outside_trading_session"
    final = quote_refresh_decision(datetime(2026, 7, 22, 7, 5, tzinfo=timezone.utc))
    assert final.run is True and final.final is True


def test_refresh_uses_complete_snapshot_without_tencent(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(module, "load_cn_symbol_catalog", lambda: _catalog())
    monkeypatch.setattr(module, "fetch_cn_quote_rows", lambda _symbols: pytest.fail("fallback must not run"))

    class _Snapshot:
        def get_snapshot(self, force=False):
            assert force is True
            return {"freshness": "fresh", "rows": [_quote("000001.SZ"), _quote("600519.SH")]}

    result = CNStockQuoteRefreshService(repository=repo, snapshot_service=_Snapshot(), cache=_Cache()).run(force=True)
    assert result["status"] == "succeeded"
    assert result["source"] == "full_snapshot"
    assert len(repo.rows) == 2


def test_refresh_falls_back_to_tencent_and_keeps_partial_success(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(module, "load_cn_symbol_catalog", lambda: _catalog())
    monkeypatch.setattr(module, "fetch_cn_quote_rows", lambda symbols: [_quote(symbols[0])])

    class _Snapshot:
        def get_snapshot(self, force=False):
            raise RuntimeError("upstream timeout")

    service = CNStockQuoteRefreshService(repository=repo, snapshot_service=_Snapshot(), cache=_Cache())
    service.batch_size = 2
    result = service.run(force=True)
    assert result == {
        "runId": "run-1", "status": "partial", "source": "tencent_batch",
        "planned": 2, "succeeded": 1, "failed": 0, "missing": 1,
    }


def test_refresh_skips_when_distributed_lock_is_held(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(module, "load_cn_symbol_catalog", lambda: pytest.fail("catalog must not load"))
    result = CNStockQuoteRefreshService(repository=repo, cache=_Cache(acquired=False)).run(force=True)
    assert result["status"] == "skipped"
    assert result["reason"] == "refresh_locked"


def test_snapshot_query_rejects_unapproved_sort_before_database_access():
    with pytest.raises(ValueError, match="invalid_sort"):
        query_cn_stock_snapshot_page(user_id=1, sort_by="latest; DROP TABLE users")


def test_overview_cache_is_derived_once_from_persisted_snapshot(monkeypatch):
    cache = _Cache()
    snapshot = {
        "rows": [_quote("000001.SZ")], "asOf": "2026-07-22T02:00:00+00:00",
        "source": "postgres-latest-snapshot", "freshness": "fresh", "status": "available",
        "warning": None,
    }
    monkeypatch.setattr(module, "load_persisted_cn_market_snapshot", lambda: snapshot)
    monkeypatch.setattr(module, "fetch_core_indices", lambda: [{"symbol": "000001.SH", "status": "available"}])
    payload = rebuild_cn_market_overview_cache(cache=cache)
    assert payload["breadth"]["coveredCount"] == 1
    assert payload["indices"][0]["symbol"] == "000001.SH"
    assert cache.values[module.OVERVIEW_CACHE_KEY] == payload


def test_schema_contains_latest_state_tables():
    schema = (module.__file__.replace("app/services/market/cn_stock_quote_snapshots.py", "migrations/init.sql"))
    text = open(schema, encoding="utf-8").read()
    assert "qd_cn_stock_quote_snapshots" in text
    assert "qd_cn_stock_quote_refresh_runs" in text
    assert "ON CONFLICT (instrument)" not in text  # runtime UPSERT belongs in repository code
