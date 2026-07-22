import pytest
from flask import Flask
from flask_smorest import Api

from app.routes import market as routes
from app.utils import auth


@pytest.fixture(scope="module")
def app():
    application = Flask(__name__)
    application.config.update(
        TESTING=True,
        API_TITLE="test",
        API_VERSION="v1",
        OPENAPI_VERSION="3.0.3",
    )
    Api(application).register_blueprint(routes.market_blp, url_prefix="/api/market")
    return application


def _headers():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda _token: {"sub": "tester", "user_id": 7, "role": "user"},
    )


def _snapshot():
    return {
        "rows": [{
            "instrument": "CNStock:600519.SH", "code": "600519", "symbol": "600519.SH",
            "exchange": "SH", "name": "贵州茅台", "latest": 101, "previousClose": 100,
            "change": 1, "changePercent": 1, "amount": 1000,
        }],
        "asOf": "2026-07-21T01:00:00Z", "source": "test", "freshness": "fresh",
        "status": "available", "warning": None,
    }


class _Snapshots:
    def get_snapshot(self):
        return _snapshot()


def test_overview_requires_login(client):
    response = client.get("/api/market/cn/overview")
    assert response.status_code == 401


def test_overview_returns_indices_and_breadth(client, monkeypatch):
    monkeypatch.setattr(routes, "get_cn_market_overview_cache", lambda: None)
    monkeypatch.setattr(routes, "load_persisted_cn_market_snapshot", lambda: None)
    monkeypatch.setattr(routes, "_cn_snapshot_service", _Snapshots())
    monkeypatch.setattr(routes, "fetch_core_indices", lambda: [{"symbol": "000001.SH", "status": "available"}])
    response = client.get("/api/market/cn/overview", headers=_headers())
    body = response.get_json()["data"]
    assert response.status_code == 200
    assert body["breadth"]["advancingCount"] == 1
    assert body["indices"][0]["symbol"] == "000001.SH"


def test_overview_prefers_persisted_snapshot_without_network_refresh(client, monkeypatch):
    snapshot = _snapshot()
    cached = {"indices": [], "breadth": {"coveredCount": 1}, "snapshot": snapshot}
    monkeypatch.setattr(routes, "get_cn_market_overview_cache", lambda: cached)

    class _MustNotRefresh:
        def get_snapshot(self):
            raise AssertionError("request path must not refresh the full-market provider")

    monkeypatch.setattr(routes, "_cn_snapshot_service", _MustNotRefresh())
    response = client.get("/api/market/cn/overview", headers=_headers())
    assert response.status_code == 200
    assert response.get_json()["data"]["breadth"]["coveredCount"] == 1


def test_catalog_validates_and_returns_user_watchlist(client, monkeypatch):
    monkeypatch.setattr(routes, "query_cn_stock_snapshot_page", lambda **_kwargs: {"coverage": {"coveredCount": 0}})
    monkeypatch.setattr(routes, "_cn_snapshot_service", _Snapshots())
    monkeypatch.setattr(routes, "load_cn_symbol_catalog", lambda **_kwargs: [{
        "instrument": "CNStock:600519.SH", "code": "600519", "symbol": "600519.SH",
        "exchange": "SH", "name": "贵州茅台",
    }])
    monkeypatch.setattr(routes, "load_cn_watchlist_symbols", lambda user_id, symbols: {"600519.SH"})
    response = client.get("/api/market/cn/stocks?page=1&pageSize=20", headers=_headers())
    assert response.status_code == 200
    assert response.get_json()["data"]["items"][0]["watchlisted"] is True
    invalid = client.get("/api/market/cn/stocks?pageSize=1000", headers=_headers())
    assert invalid.status_code == 400
    invalid_sort = client.get("/api/market/cn/stocks?sortBy=latest%3BDROP", headers=_headers())
    assert invalid_sort.status_code == 400


def test_detail_and_history_reject_unknown_symbol(client, monkeypatch):
    monkeypatch.setattr(routes, "load_cn_symbol", lambda _symbol: None)
    assert client.get("/api/market/cn/stocks/830001", headers=_headers()).status_code == 404
    assert client.get("/api/market/cn/stocks/830001/history", headers=_headers()).status_code == 404


def test_detail_and_history_return_service_payload(client, monkeypatch):
    identity = {
        "instrument": "CNStock:600519.SH", "code": "600519", "symbol": "600519.SH",
        "exchange": "SH", "name": "贵州茅台",
    }

    class _Detail:
        def detail(self, received, watchlisted=False):
            return {**received, "watchlisted": watchlisted}

        def history(self, received, limit=260, adjustment="forward"):
            return {"bars": [{"date": "2026-07-20"}], "provenance": {"backtestEligible": False}}

    monkeypatch.setattr(routes, "load_cn_symbol", lambda _symbol: identity)
    monkeypatch.setattr(routes, "load_cn_watchlist_symbols", lambda *_args: {"600519.SH"})
    monkeypatch.setattr(routes, "_cn_detail_service", _Detail())
    detail = client.get("/api/market/cn/stocks/600519.SH", headers=_headers())
    history = client.get("/api/market/cn/stocks/600519.SH/history", headers=_headers())
    assert detail.get_json()["data"]["watchlisted"] is True
    assert history.get_json()["data"]["provenance"]["backtestEligible"] is False
