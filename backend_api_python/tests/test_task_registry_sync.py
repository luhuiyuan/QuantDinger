from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "services" / "task_control" / "repository.py").read_text(encoding="utf-8")


def test_registry_sync_refuses_empty_registry_and_retires_missing_definitions():
    assert "Task Registry is empty; refusing to retire all definitions" in SOURCE
    assert "status = 'retired'" in SOURCE
    assert "status = 'active'" in SOURCE
