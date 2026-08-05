from types import SimpleNamespace

import pytest

from app.routes import data_source_operations as routes
from app.services.data_routing.credentials import CredentialInstanceContext, StoredCredentialSecret
from app.services.data_routing.diagnostics import ProviderDiagnosticError
from app.services.data_routing.diagnostics import PostgresDiagnosticRepository


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
