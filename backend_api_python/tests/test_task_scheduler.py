from datetime import datetime, timezone
from dataclasses import replace

from app.services.task_control.repository import ScheduleDecision
from app.services.task_control.scheduler import TaskScheduler


class FakeRepository:
    def __init__(self, decisions):
        self.decisions = tuple(decisions)
        self.created = []
        self.events = []

    def scan_due_schedules(self, *, exclusivity_key):
        assert callable(exclusivity_key)
        outcomes = {"normal": "enqueued", "overlap": "overlap", "missed": "missed"}
        return tuple(replace(item, outcome=outcomes[item.task_key]) for item in self.decisions)

    def mark_stale_runs(self, **kwargs):
        assert kwargs == {"stale_after_seconds": 180}
        return 2

    def append_schedule_event(self, **kwargs):
        self.events.append(kwargs)
        return len(self.events)


def _decision(task_key, skip_reason=""):
    now = datetime.now(timezone.utc)
    return ScheduleDecision(1, task_key, "1", {}, 2, now, now, "Asia/Shanghai", skip_reason)


def test_scheduler_enqueues_due_run_and_logs_overlap_or_missed_skip():
    repo = FakeRepository([_decision("normal"), _decision("overlap"), _decision("missed", "missed_skipped")])
    scheduler = TaskScheduler(repository=repo, exclusivity_key=lambda item: item.task_key)

    summary = scheduler.scan_once()

    assert summary.scanned == 3
    assert summary.worker_lost == 2
    assert summary.enqueued == 1
    assert summary.overlap_skipped == 1
    assert summary.skipped == 1
    assert [event["event_type"] for event in repo.events] == ["overlap_skipped", "missed_skipped"]
