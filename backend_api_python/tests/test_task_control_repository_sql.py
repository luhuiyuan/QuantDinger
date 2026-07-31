from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "services" / "task_control" / "repository.py").read_text(encoding="utf-8")


def test_claim_query_uses_priority_fifo_and_skip_locked():
    assert "ORDER BY priority ASC, created_at ASC, id ASC" in SOURCE
    assert "FOR UPDATE SKIP LOCKED" in SOURCE


def test_lease_defaults_and_worker_lost_contract_are_explicit():
    assert "lease_seconds: int = 60" in SOURCE
    assert "stale_after_seconds: int = 180" in SOURCE
    assert "worker_lost" in SOURCE


def test_event_writer_requires_valid_lease_and_bounds_metadata():
    assert "Task Run lease is missing or expired" in SOURCE
    assert "16 * 1024" in SOURCE
    assert "metadata_truncated" in SOURCE
    assert "_SENSITIVE_METADATA_KEYS" in SOURCE
