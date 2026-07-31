"""Tests for the agent_jobs progress / streaming machinery.

These tests avoid hitting Postgres by stubbing the persistence helpers in
`agent_jobs`. They focus on the behavior that matters to SSE clients:
  * Runner-signature detection (single-arg vs. on_progress).
  * Progress events accumulate in monotonic order.
  * Terminal events stop the stream and clean up state.
  * Idle timeout returns control without hanging.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.utils import agent_jobs
from app.services.task_control.repository import TaskRunRecord


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    """Replace DB-touching helpers with no-ops so tests run without Postgres."""
    monkeypatch.setattr(agent_jobs, "_set_status", lambda *a, **kw: None)
    monkeypatch.setattr(agent_jobs, "_set_result", lambda *a, **kw: None)
    monkeypatch.setattr(agent_jobs, "_set_failure", lambda *a, **kw: None)
    monkeypatch.setattr(agent_jobs, "get_job_for_worker", lambda *_: None)

    def _fake_publish(job_id, event, *, terminal=False):
        # Re-implement publish without DB persistence.
        buf, lock = agent_jobs._job_buffer(job_id)
        with lock:
            seq = (buf[-1]["seq"] + 1) if buf else 1
            buf.append({"seq": seq, "ts": time.time(), "data": event, "terminal": terminal})
        agent_jobs._job_signal(job_id).set()

    monkeypatch.setattr(agent_jobs, "_publish_progress", _fake_publish)

    yield

    agent_jobs._progress_buffers.clear()
    agent_jobs._progress_locks.clear()
    agent_jobs._progress_signals.clear()


def test_runner_accepts_progress_detection():
    def one_arg(payload):
        return payload

    def two_args(payload, on_progress):
        on_progress({"ok": True})
        return payload

    assert agent_jobs._runner_accepts_progress(one_arg) is False
    assert agent_jobs._runner_accepts_progress(two_args) is True


def test_publish_and_stream_orderly():
    job_id = "job-test-stream-1"
    agent_jobs._publish_progress(job_id, {"phase": "a"})
    agent_jobs._publish_progress(job_id, {"phase": "b"})
    agent_jobs._publish_progress(job_id, {"phase": "done"}, terminal=True)

    events = list(agent_jobs.stream_progress(job_id, since_seq=0, idle_timeout_s=2.0))
    seqs = [e["seq"] for e in events]
    assert seqs == [1, 2, 3]
    assert events[-1]["terminal"] is True
    # Terminal cleanup should release the per-job state.
    assert job_id not in agent_jobs._progress_buffers


def test_stream_resumes_from_since_seq():
    job_id = "job-test-stream-2"
    for i in range(5):
        agent_jobs._publish_progress(job_id, {"i": i})
    agent_jobs._publish_progress(job_id, {"end": True}, terminal=True)

    events = list(agent_jobs.stream_progress(job_id, since_seq=3, idle_timeout_s=2.0))
    assert [e["seq"] for e in events] == [4, 5, 6]


def test_stream_idle_timeout_returns():
    job_id = "job-test-idle"
    # No events ever — generator should give up after idle_timeout.
    t0 = time.monotonic()
    events = list(agent_jobs.stream_progress(job_id, since_seq=0, idle_timeout_s=0.3))
    elapsed = time.monotonic() - t0
    assert events == []
    # 5s wait cap inside the loop, but we asked for 0.3s budget overall.
    assert elapsed < 1.5


def test_stream_picks_up_live_event():
    """Producer thread emits one event after a short delay; consumer must see it."""
    job_id = "job-test-live"

    def _producer():
        time.sleep(0.05)
        agent_jobs._publish_progress(job_id, {"hello": "world"}, terminal=True)

    threading.Thread(target=_producer, daemon=True).start()
    events = list(agent_jobs.stream_progress(job_id, since_seq=0, idle_timeout_s=2.0))
    assert len(events) == 1
    assert events[0]["data"]["hello"] == "world"
    assert events[0]["terminal"] is True


def test_backtest_mutex_reuses_same_user_run_but_separates_users(monkeypatch):
    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            return None

    class Repository:
        active = {}

        def get_active_run_by_exclusivity(self, key):
            return self.active.get(key)

        def create_run(self, **kwargs):
            existing = self.active.get(kwargs["exclusivity_key"])
            if existing:
                return existing, False
            run = TaskRunRecord(
                f"run-{kwargs['domain_run_id']}", kwargs["task_key"], kwargs["definition_version"],
                "queued", kwargs["exclusivity_key"], kwargs["parameters"], kwargs["priority"],
                owner_user_id=kwargs["owner_user_id"], domain_kind=kwargs["domain_kind"],
                domain_run_id=kwargs["domain_run_id"],
            )
            self.active[kwargs["exclusivity_key"]] = run
            return run, True

    ids = iter(("job-user-1", "job-user-2"))
    monkeypatch.setattr(agent_jobs, "_new_job_id", lambda: next(ids))
    monkeypatch.setattr(agent_jobs, "get_db_connection", lambda: Database())
    monkeypatch.setattr("app.services.task_control.repository.TaskControlRepository", Repository)

    first = agent_jobs.submit_job(
        user_id=1, agent_token_id=None, kind="backtest", request_payload={}, runner=lambda payload: payload,
    )
    duplicate = agent_jobs.submit_job(
        user_id=1, agent_token_id=None, kind="backtest", request_payload={}, runner=lambda payload: payload,
    )
    second_user = agent_jobs.submit_job(
        user_id=2, agent_token_id=None, kind="backtest", request_payload={}, runner=lambda payload: payload,
    )

    assert first["created"] is True
    assert duplicate == {"job_id": "job-user-1", "status": "queued", "kind": "backtest", "created": False}
    assert second_user["created"] is True
    assert second_user["job_id"] == "job-user-2"
