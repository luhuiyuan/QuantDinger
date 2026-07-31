from pathlib import Path


SCHEMA = (Path(__file__).resolve().parents[1] / "migrations" / "init.sql").read_text(encoding="utf-8")


def test_internal_task_control_tables_are_declared():
    for table in (
        "qd_task_definitions",
        "qd_task_schedules",
        "qd_task_runs",
        "qd_task_events",
        "qd_task_leases",
        "qd_task_audit",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA


def test_task_run_state_and_active_exclusivity_constraints_are_declared():
    assert "'cancel_requested'" in SCHEMA
    assert "'retry_wait'" in SCHEMA
    assert "idx_task_runs_active_exclusivity" in SCHEMA
    assert "WHERE status IN ('queued', 'running', 'retry_wait', 'cancel_requested')" in SCHEMA


def test_task_event_requires_run_or_schedule_reference():
    assert "CHECK (run_id IS NOT NULL OR schedule_id IS NOT NULL)" in SCHEMA
    assert "idx_task_events_run_time" in SCHEMA


def test_definition_reserves_task_level_concurrency_and_progress_timestamp():
    assert "max_concurrency INTEGER" in SCHEMA
    assert "last_progress_at TIMESTAMPTZ" in SCHEMA
    assert "ADD COLUMN IF NOT EXISTS max_concurrency INTEGER" in SCHEMA
    assert "ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ" in SCHEMA
