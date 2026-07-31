from pathlib import Path


SCHEMA = (Path(__file__).resolve().parents[1] / "migrations" / "init.sql").read_text(encoding="utf-8")
SOURCE = (Path(__file__).resolve().parents[1] / "app" / "services" / "task_control" / "repository.py").read_text(encoding="utf-8")


def test_schedule_occurrence_is_uniquely_deduplicated():
    assert "idx_task_runs_schedule_scheduled" in SCHEMA
    assert "ON qd_task_runs(schedule_id, scheduled_at)" in SCHEMA


def test_scheduler_scan_uses_transaction_advisory_lock():
    assert "pg_try_advisory_xact_lock" in SOURCE
    assert "missed_skipped" in SOURCE


def test_schedule_cursor_and_run_insert_share_one_transaction():
    method = SOURCE[SOURCE.index("def scan_due_schedules"):SOURCE.index("def create_run")]
    assert "INSERT INTO qd_task_runs" in method
    assert method.index("INSERT INTO qd_task_runs") < method.rindex("db.commit()")
    assert "current if skip_reason else scheduled_at" in method
