# Internal Task Scheduler replaces Celery

**Status: accepted**

QuantDinger will migrate all finite Celery tasks in phases to an internal Task
Scheduler and executor hosted in the existing `scheduler-worker` container,
then remove Celery Worker, Celery Beat, `redis-jobs`, and Celery-only code once
all hard migration gates pass. The system keeps generic task-control tables
alongside domain run tables, uses code-registered versioned Task Definitions,
database advisory locks plus Run leases, and runs each Task Run in an isolated
subprocess. This deliberately trades Celery's mature distributed execution and
ecosystem for a domain-owned lifecycle, checkpoint/cancellation semantics, and
operator-visible task management; immediate removal is allowed only after
verification of scheduling, retries, cancellation, worker-loss handling,
permissions, audit, and UI behavior.

## Considered options

- Keep Celery as the permanent execution backend.
- Run Celery and the internal executor as long-term dual backends.
- Replace Celery with the internal system and remove Celery after all hard
  gates pass (chosen).

## Consequences

Running Task Runs are never transferred between executors. A migration
interruption is recorded as `failed/migration_interrupted`; an administrator
must inspect the checkpoint and manually create a new internal retry Run.
