from pathlib import Path

from app.services.external_data_request_logs import (
    ExternalDataRequestLog,
    ExternalDataRequestResult,
    ProviderAttempt,
    sanitize_summary,
)
from app.services.external_data_request_settings import load_external_data_request_log_settings
from app.data_providers import _cache_metric_scope
from app.data_sources import tencent
from app.services import external_data_request_logs as request_log_module


def test_migration_defines_idempotent_observability_tables_and_indexes():
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "20260727_external_data_request_logs.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS qd_external_data_request_logs" in sql
    assert "qd_external_data_cache_stats" in sql
    assert "qd_external_data_log_cleanup_runs" in sql
    assert "idx_external_request_logs_provider_result_time" in sql
    assert "idx_external_request_logs_retention" in sql


def test_sanitize_summary_removes_secrets_url_query_and_truncates():
    text = sanitize_summary({
        "api_key": "should-not-survive",
        "Authorization": "Bearer should-not-survive",
        "url": "https://provider.example/quote?symbol=BTC&token=should-not-survive",
    })
    assert "should-not-survive" not in text
    assert "[REDACTED]" in text
    assert "?" not in text
    assert len(sanitize_summary("x" * 2000, max_length=32)) == 32


def test_log_entry_normalizes_only_safe_summary_and_known_result():
    payload = ExternalDataRequestLog(
        provider="finnhub",
        data_domain="quote",
        operation="get_quote",
        result=ExternalDataRequestResult.SUCCESS,
        subject_summary="https://provider.example/path?api_key=secret",
        error_summary="Authorization: Bearer secret",
    ).normalized()
    assert payload["result"] == "success"
    assert payload["subject_summary"] == "https://provider.example/path"
    assert "secret" not in payload["error_summary"]


def test_log_settings_defaults_and_safety_bounds(monkeypatch):
    monkeypatch.delenv("EXTERNAL_DATA_REQUEST_LOG_SUCCESS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("EXTERNAL_DATA_REQUEST_LOG_ERROR_RETENTION_DAYS", raising=False)
    defaults = load_external_data_request_log_settings()
    assert defaults.successful_retention_days == 30
    assert defaults.error_retention_days == 90

    monkeypatch.setenv("EXTERNAL_DATA_REQUEST_LOG_SUCCESS_RETENTION_DAYS", "1")
    monkeypatch.setenv("EXTERNAL_DATA_REQUEST_LOG_ERROR_RETENTION_DAYS", "9999")
    monkeypatch.setenv("EXTERNAL_DATA_REQUEST_LOG_CLEANUP_BATCH_SIZE", "bad")
    bounded = load_external_data_request_log_settings()
    assert bounded.successful_retention_days == 7
    assert bounded.error_retention_days == 730
    assert bounded.cleanup_batch_size == 500


class _RecordingService:
    def __init__(self):
        self.entries = []

    def record(self, entry):
        self.entries.append(entry)
        return True


def test_tencent_transport_does_not_write_duplicate_legacy_attempts(monkeypatch):
    service = _RecordingService()
    monkeypatch.setattr(request_log_module, "ExternalDataRequestLogService", lambda: service)
    monkeypatch.setattr(tencent, "_fetch_kline_raw", lambda *args, **kwargs: [["2026-07-27", "1", "2", "1", "2", "10"]])

    assert tencent.fetch_kline("SZ000012", "day", count=260)
    assert service.entries == []

    monkeypatch.setattr(tencent, "_fetch_kline_raw", lambda *args, **kwargs: [])
    assert tencent.fetch_kline("SZ000012", "day") == []
    assert service.entries == []


def test_provider_attempt_records_timeout_without_suppressing_original_error():
    service = _RecordingService()
    try:
        with ProviderAttempt(
            provider="finnhub",
            data_domain="quote",
            operation="get_quote",
            fallback_index=1,
            retry_count=2,
            service=service,
        ):
            raise TimeoutError("token=should-not-survive")
    except TimeoutError:
        pass
    else:
        raise AssertionError("ProviderAttempt must not suppress data-provider errors")

    assert len(service.entries) == 1
    entry = service.entries[0].normalized()
    assert entry["result"] == "timeout"
    assert entry["fallback_index"] == 1
    assert entry["retry_count"] == 2
    assert "should-not-survive" not in entry["error_summary"]


def test_provider_attempt_supports_controlled_skip_and_http_rate_limit():
    service = _RecordingService()
    with ProviderAttempt(provider="fred", data_domain="macro", operation="series", service=service) as attempt:
        attempt.set_http_status(429)
    assert service.entries[-1].normalized()["result"] == "rate_limited"

    with ProviderAttempt(provider="fred", data_domain="macro", operation="series", service=service) as attempt:
        attempt.skip(disabled=True, reason="provider disabled")
    assert service.entries[-1].normalized()["result"] == "disabled"


def test_cache_metric_scope_keeps_cache_hits_aggregated_and_domain_scoped():
    assert _cache_metric_scope("economic_calendar_v3") == ("calendar", "economic_calendar_v3")
    assert _cache_metric_scope("market_news") == ("news", "market_news")
    assert _cache_metric_scope("forex_pairs") == ("market", "forex_pairs")
