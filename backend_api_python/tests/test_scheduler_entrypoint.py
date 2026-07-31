from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "commands" / "scheduler.py").read_text(encoding="utf-8")


def test_scheduler_entrypoint_scans_finite_tasks_every_five_seconds_without_coupling_domain_loop():
    assert "task_scheduler.scan_once()" in SOURCE
    assert "shutdown.event.wait(5)" in SOURCE
    assert 'logger.error("Task Scheduler scan failed", exc_info=True)' in SOURCE
    assert 'name="TaskExecutorParent"' in SOURCE
    assert "task_executor.execute_one()" in SOURCE
    assert "ensure_builtin_schedules(BUILTIN_SCHEDULES)" in SOURCE
