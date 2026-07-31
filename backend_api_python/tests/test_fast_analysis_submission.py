from app.services import fast_analysis_tasks
from app.services.task_control.repository import TaskControlRepository


def test_async_submission_guard_uses_cross_process_advisory_lock(monkeypatch):
    calls = []

    class Guard:
        def __enter__(self):
            calls.append("enter")
            return True

        def __exit__(self, exc_type, exc, traceback):
            calls.append("exit")

    monkeypatch.setattr(TaskControlRepository, "submission_lock", lambda self, key: Guard())
    key = "7|CNSTOCK|600519.SH|1D"

    assert fast_analysis_tasks.acquire_inflight(key, distributed=True) is True
    fast_analysis_tasks.release_inflight(key)

    assert calls == ["enter", "exit"]


def test_fast_analysis_task_mutex_normalizes_case():
    assert fast_analysis_tasks.build_task_exclusivity_key(7, "cnstock", "600519.sh", "1d") == (
        fast_analysis_tasks.build_task_exclusivity_key(7, "CNStock", "600519.SH", "1D")
    )
