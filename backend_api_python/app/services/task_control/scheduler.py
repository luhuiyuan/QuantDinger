"""Finite Task Scheduler scan loop primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .repository import ScheduleDecision, TaskControlRepository


@dataclass(frozen=True, slots=True)
class ScanSummary:
    worker_lost: int = 0
    scanned: int = 0
    enqueued: int = 0
    skipped: int = 0
    overlap_skipped: int = 0


class TaskScheduler:
    def __init__(
        self,
        *,
        repository: TaskControlRepository,
        exclusivity_key: Callable[[ScheduleDecision], str],
    ) -> None:
        self.repository = repository
        self._exclusivity_key = exclusivity_key

    def scan_once(self) -> ScanSummary:
        worker_lost = self.repository.mark_stale_runs(stale_after_seconds=180)
        decisions = self.repository.scan_due_schedules(exclusivity_key=self._exclusivity_key)
        enqueued = skipped = overlap_skipped = 0
        for decision in decisions:
            if decision.skip_reason:
                self.repository.append_schedule_event(
                    schedule_id=decision.schedule_id,
                    event_type=decision.skip_reason,
                    message="Cron occurrence skipped while scheduler was unavailable",
                    metadata={"scheduled_at": decision.scheduled_at.isoformat()},
                )
                skipped += 1
                continue
            if decision.outcome == "enqueued":
                enqueued += 1
            elif decision.outcome == "overlap":
                self.repository.append_schedule_event(
                    schedule_id=decision.schedule_id,
                    event_type="overlap_skipped",
                    message="Active Run already owns the task exclusivity key",
                    metadata={"scheduled_at": decision.scheduled_at.isoformat()},
                )
                overlap_skipped += 1
        return ScanSummary(
            worker_lost=worker_lost,
            scanned=len(decisions),
            enqueued=enqueued,
            skipped=skipped,
            overlap_skipped=overlap_skipped,
        )
