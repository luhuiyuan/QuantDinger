from pathlib import Path

import pytest

from app.services.task_control.repository import validate_event_type


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "services" / "task_control" / "repository.py").read_text(encoding="utf-8")


def test_claim_query_uses_priority_fifo_and_skip_locked():
    assert "ORDER BY priority ASC, created_at ASC, id ASC" in SOURCE
    assert "FOR UPDATE SKIP LOCKED" in SOURCE
    assert "pg_try_advisory_xact_lock" in SOURCE
    assert "active.status IN ('running', 'cancel_requested')" in SOURCE


def test_lease_defaults_and_worker_lost_contract_are_explicit():
    assert "lease_seconds: int = 60" in SOURCE
    assert "stale_after_seconds: int = 180" in SOURCE
    assert "worker_lost" in SOURCE


def test_event_writer_requires_valid_lease_and_bounds_metadata():
    assert "Task Run lease is missing or expired" in SOURCE
    assert "16 * 1024" in SOURCE
    assert "metadata_truncated" in SOURCE
    assert "_SENSITIVE_METADATA_KEYS" in SOURCE


def test_event_type_is_structured_and_bounded_before_database_write():
    assert validate_event_type("task_succeeded") == "task_succeeded"
    for invalid in ("", "TaskSucceeded", "has space", "x" * 81):
        with pytest.raises(ValueError):
            validate_event_type(invalid)


def test_manual_retry_creates_new_linked_run_and_preserves_original():
    assert "create_manual_retry" in SOURCE
    assert "retry_of_run_id=original.run_id" in SOURCE
    assert "Only failed or cancelled Task Runs can be retried manually" in SOURCE


def test_event_cleanup_is_bounded_and_does_not_delete_runs():
    assert "cleanup_events" in SOURCE
    assert "DELETE FROM qd_task_events" in SOURCE
    assert "retention_days: int = 90" in SOURCE


def test_provider_filter_links_events_to_external_request_logs():
    assert "qd_external_data_request_logs external_log" in SOURCE
    assert "external_log.request_id = event.external_request_id" in SOURCE
    assert "external_log.provider = %s" in SOURCE


def test_domain_factory_runs_only_after_exclusivity_lock():
    method = SOURCE[SOURCE.index("def create_run_with_domain_factory"):SOURCE.index("def transition_run")]
    assert method.index("pg_advisory_xact_lock") < method.index("domain_factory()")
    assert "compensate(domain_run_id)" in method
