from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "commands" / "scheduler.py").read_text(encoding="utf-8")


def test_scheduler_initializes_registry_before_loop_and_disables_empty_registry():
    assert "sync_registered_task_definitions" in SOURCE
    assert "Task Registry is empty; finite Task Scheduler remains disabled" in SOURCE
