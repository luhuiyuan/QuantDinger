from pathlib import Path

import pytest

from app.services.task_control import executor as executor_module
from app.services.task_control.executor import ChildReporter, TaskExecutor
from app.services.task_control.registry import TaskDefinition, TaskRegistry
from app.services.task_control.repository import TaskRunRecord


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "services" / "task_control" / "executor.py").read_text(encoding="utf-8")


def test_executor_module_imports():
    assert TaskExecutor is not None
    assert ChildReporter is not None


def test_executor_uses_isolated_spawn_process_and_controlled_ipc():
    assert 'get_context("spawn")' in SOURCE
    assert "multiprocessing.Pipe" in SOURCE
    assert "ChildReporter" in SOURCE
    assert "claim_next" in SOURCE


def test_executor_parent_owns_lease_and_final_state():
    assert "renew_lease" in SOURCE
    assert "now - last_heartbeat >= 20" in SOURCE
    assert "release_lease" in SOURCE
    assert 'transition_run(run.run_id, "succeeded"' in SOURCE
    assert 'transition_run(run.run_id, "failed"' in SOURCE


def test_executor_does_not_silently_upgrade_definition_version():
    assert "definition_version_unavailable" in SOURCE
    assert "definition.definition_version != run.definition_version" in SOURCE


def test_executor_uses_cooperative_cancel_before_forced_interruption():
    assert "cancel_event.set()" in SOURCE
    assert "definition.cancellation_handler" in SOURCE
    assert "cancel_grace_seconds" in SOURCE
    assert 'error_code="forced_interruption"' in SOURCE


def test_executor_supports_retry_timeout_and_progress_throttling():
    assert "is_temporary_error" in SOURCE
    assert "schedule_retry" in SOURCE
    assert 'error_code="timed_out"' in SOURCE
    assert "now - last_progress_write >= 5" in SOURCE


class _Connection:
    def poll(self, timeout=0):
        return False

    def close(self):
        pass


class _CancelEvent:
    def __init__(self):
        self.requested = False

    def set(self):
        self.requested = True

    def is_set(self):
        return self.requested


class _Process:
    last_created = None

    def __init__(self, *, args, mode):
        type(self).last_created = self
        self.cancel_event = args[4]
        self.mode = mode
        self.exitcode = 7 if mode == "crash" else 0
        self.terminated = False
        self.killed = False

    def start(self):
        pass

    def is_alive(self):
        if self.mode == "crash":
            return False
        if self.killed and self.mode != "unkillable":
            return False
        if self.terminated and self.mode not in {"resistant", "unkillable"}:
            return False
        if self.mode == "cooperative":
            return not self.cancel_event.is_set()
        return True

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def join(self, timeout=None):
        pass


class _Context:
    def __init__(self, mode):
        self.mode = mode

    def Event(self):
        return _CancelEvent()

    def Process(self, *, target, args, daemon):
        assert daemon is False
        return _Process(args=args, mode=self.mode)


class _Repository:
    def __init__(self, current_status="running"):
        self.run = TaskRunRecord("run-1", "task", "1", "running", "task", {}, 2, attempt_no=1)
        self.current_status = current_status
        self.transitions = []
        self.events = []
        self.released = False

    def claim_next(self, **kwargs):
        return self.run

    def get_run(self, run_id):
        return TaskRunRecord("run-1", "task", "1", self.current_status, "task", {}, 2, attempt_no=1)

    def transition_run(self, run_id, status, **kwargs):
        self.transitions.append((status, kwargs))
        self.current_status = status
        return self.get_run(run_id)

    def release_lease(self, **kwargs):
        self.released = True
        return True

    def renew_lease(self, **kwargs):
        return True

    def append_event(self, **kwargs):
        self.events.append(kwargs)
        return len(self.events)


def _executor(monkeypatch, repository, *, mode, capabilities=None, cancellation_handler=None):
    registry = TaskRegistry()
    registry.register(TaskDefinition(
        "task", "1", lambda parameters, reporter: {},
        capabilities=capabilities or {}, cancellation_handler=cancellation_handler,
    ))
    monkeypatch.setattr(executor_module.multiprocessing, "Pipe", lambda duplex=False: (_Connection(), _Connection()))
    monkeypatch.setattr(executor_module.multiprocessing, "get_context", lambda method: _Context(mode))
    return TaskExecutor(repository=repository, registry=registry, holder_id="holder")


def test_child_process_crash_is_failed_and_lease_is_released(monkeypatch):
    repo = _Repository()
    result = _executor(monkeypatch, repo, mode="crash").execute_one()
    assert result.status == "failed"
    assert repo.transitions[-1][1]["error_code"] == "child_process_exit"
    assert repo.events[-1]["event_type"] == "task_failed"
    assert repo.released is True


def test_event_persistence_failure_does_not_block_terminal_transition(monkeypatch):
    class EventFailingRepository(_Repository):
        def append_event(self, **kwargs):
            raise ValueError("event rejected")

    repo = EventFailingRepository()
    result = _executor(monkeypatch, repo, mode="crash").execute_one()
    assert result.status == "failed"
    assert repo.transitions[-1][0] == "failed"
    assert repo.released is True


def test_cooperative_cancel_ends_cancelled(monkeypatch):
    cancelled = []
    repo = _Repository(current_status="cancel_requested")
    result = _executor(
        monkeypatch, repo, mode="cooperative",
        cancellation_handler=lambda parameters: cancelled.append(True),
    ).execute_one()
    assert result.status == "cancelled"
    assert cancelled == [True]
    assert repo.transitions[-1][0] == "cancelled"
    assert repo.events[-1]["event_type"] == "task_cancelled"


def test_cancel_timeout_forces_child_termination(monkeypatch):
    repo = _Repository(current_status="cancel_requested")
    ticks = iter(range(0, 100, 2))
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: next(ticks))
    result = _executor(
        monkeypatch, repo, mode="stubborn", capabilities={"cancel_grace_seconds": 1},
    ).execute_one()
    assert result.status == "failed"
    assert repo.transitions[-1][1]["error_code"] == "forced_interruption"


def test_cancel_timeout_kills_child_that_ignores_terminate(monkeypatch):
    repo = _Repository(current_status="cancel_requested")
    ticks = iter(range(0, 100, 2))
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: next(ticks))
    result = _executor(
        monkeypatch, repo, mode="resistant", capabilities={"cancel_grace_seconds": 1},
    ).execute_one()
    assert result.status == "failed"
    assert _Process.last_created.terminated is True
    assert _Process.last_created.killed is True
    assert repo.released is True


def test_executor_keeps_lease_when_child_survives_kill(monkeypatch):
    repo = _Repository(current_status="cancel_requested")
    ticks = iter(range(0, 100, 2))
    monkeypatch.setattr(executor_module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="remained alive"):
        _executor(
            monkeypatch, repo, mode="unkillable", capabilities={"cancel_grace_seconds": 1},
        ).execute_one()
    assert repo.released is False
