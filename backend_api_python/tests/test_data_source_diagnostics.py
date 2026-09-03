from types import SimpleNamespace
import json
import math

import pytest

from app.routes import data_source_operations as routes
from app.services.data_routing.credentials import CredentialInstanceContext, StoredCredentialSecret
from app.services.data_routing.diagnostics import ProviderDiagnosticError
from app.services.data_routing.diagnostics import PostgresDiagnosticRepository
from app.services.data_routing.diagnostics import _safe_sample_value, _sample_preview


class _Credentials:
    def __init__(self, context, credential):
        self.context = context
        self.credential = credential

    def get_instance_context(self, instance_id):
        assert instance_id == 11
        return self.context

    def get_active_credential(self, instance_id):
        assert instance_id == 11
        return self.credential


class _Management:
    def get_instance(self, instance_id):
        assert instance_id == 11
        return {
            "instance_key": "default_easy_tdx",
            "display_name": "Default EasyTDX",
            "capabilities": [{"capability_key": "asia_equity_kline", "eligibility_status": "eligible"}],
        }


def _patch_direct_diagnostic_dependencies(monkeypatch, *, lifecycle_status="active", credential=True):
    context = CredentialInstanceContext(
        instance_id=11, adapter_key="easy_tdx", lifecycle_status=lifecycle_status,
        provider_account_identity=None, non_secret_config={"market": "cn"},
    )
    secret = StoredCredentialSecret(credential_id=42, credential_version=1,
                                    credential_schema_version="1", status="active",
                                    encryption_key_id="key", ciphertext="encrypted") if credential else None
    monkeypatch.setattr(routes, "PostgresProviderCredentialRepository", lambda: _Credentials(context, secret))
    monkeypatch.setattr(routes, "PostgresDataSourceManagementRepository", _Management)
    monkeypatch.setattr(
        routes, "load_default_data_routing_registry",
        lambda: SimpleNamespace(adapters={"easy_tdx": SimpleNamespace(capabilities={"asia_equity_kline"})}),
    )


def test_direct_diagnostic_entry_allows_configured_instance_outside_published_route(monkeypatch):
    _patch_direct_diagnostic_dependencies(monkeypatch)

    entry = routes._direct_diagnostic_entry(11, "asia_equity_kline")

    assert entry.instance_id == 11
    assert entry.instance_key == "default_easy_tdx"
    assert entry.eligibility_status == "eligible"
    assert entry.secret_handle.resolve_credential_id() == 42


def test_direct_diagnostic_entry_explains_missing_active_credential(monkeypatch):
    _patch_direct_diagnostic_dependencies(monkeypatch, credential=False)

    with pytest.raises(ProviderDiagnosticError, match="no active credential"):
        routes._direct_diagnostic_entry(11, "asia_equity_kline")


def test_direct_diagnostic_entry_rejects_retired_instance(monkeypatch):
    _patch_direct_diagnostic_dependencies(monkeypatch, lifecycle_status="retired")

    with pytest.raises(ProviderDiagnosticError, match="Retired"):
        routes._direct_diagnostic_entry(11, "asia_equity_kline")


def test_latest_diagnostics_returns_only_safe_persisted_fields():
    class Cursor:
        def execute(self, query, params):
            assert "DISTINCT ON (capability_key)" in query
            assert params == (11,)

        def fetchall(self):
            return [{
                "diagnostic_id": "test-1", "instance_id": 11, "capability_key": "asia_equity_kline",
                "status": "succeeded", "created_at": "created", "completed_at": "completed",
                "sanitized_result": {
                    "succeeded": True, "sample": {"rows": [{"close": 10}]},
                    "request_summary": {"subject": {"symbol": "600000"}}, "unexpected": "never returned",
                },
            }]

        def close(self):
            return None

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def cursor(self): return Cursor()

    item = PostgresDiagnosticRepository(lambda: Connection()).latest_for_instance(11)[0]

    assert item["diagnostic_id"] == "test-1"
    assert item["sample"] == {"rows": [{"close": 10}]}
    assert "unexpected" not in item


def test_successful_diagnostic_marks_its_capability_eligible_without_touching_health_state():
    class Cursor:
        def __init__(self):
            self.queries = []

        def execute(self, query, params):
            self.queries.append((query, params))

        def close(self):
            return None

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            self.committed = True

    connection = Connection()
    PostgresDiagnosticRepository(lambda: connection).complete(
        "test-1", status="succeeded", result={"succeeded": True},
    )

    assert connection.committed is True
    assert len(connection.cursor_instance.queries) == 2
    update_query, update_params = connection.cursor_instance.queries[1]
    assert "UPDATE qd_provider_instance_capabilities" in update_query
    assert "eligibility_status='eligible'" in update_query
    assert "qd_provider_health_states" not in update_query
    assert update_params == ("test-1", "test-1")


def test_daily_bar_page_diagnostic_preview_contains_structured_ohlc_rows():
    from datetime import date, datetime, timezone
    from decimal import Decimal

    from app.services.cn_market_history.instruments import parse_cn_instrument
    from app.services.cn_market_history.models import RawDailyBar
    from app.services.cn_market_history.tdx_provider import DailyBarPage

    bar = RawDailyBar(
        instrument=parse_cn_instrument("600000.SH"), trade_date=date(2025, 1, 2),
        open=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10.5"),
        volume=Decimal("100"), amount=Decimal("1000"), provider="easy_tdx",
        provider_version="test", content_hash="hash", collected_at=datetime.now(timezone.utc),
    )
    page = DailyBarPage(offset=0, next_offset=1, raw_count=1, bars=(bar,), reached_start=True)
    preview = _sample_preview(page, kind="history")
    assert {"trade_date", "open", "high", "low", "close", "volume"}.issubset(preview["columns"])
    assert preview["rows"][0]["trade_date"] == "2025-01-02"
    assert preview["rows"][0]["close"] == "10.5"


def test_diagnostic_preview_is_json_safe_for_non_finite_provider_values():
    preview = _safe_sample_value({"cash_dividend": math.nan, "rights_price": math.inf})

    assert preview == {"cash_dividend": None, "rights_price": None}
    json.dumps(preview, allow_nan=False)
