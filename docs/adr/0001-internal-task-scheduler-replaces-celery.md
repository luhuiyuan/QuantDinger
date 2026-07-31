# Internal Task Scheduler replaces Celery

**Status: accepted**

QuantDinger will implement and validate all finite-task adapters in phases, then
perform one hard production cutover to an internal Task Scheduler and executor
hosted in the existing `scheduler-worker` container. The final production
version contains no Celery compatibility layer, runtime backend switch, or
fallback: after all hard gates pass it removes Celery Worker, Celery Beat,
`redis-jobs`, and Celery-only code. The system keeps generic task-control tables
alongside domain run tables, uses code-registered versioned Task Definitions,
database advisory locks plus Run leases, and runs each Task Run in an isolated
subprocess. This deliberately trades Celery's mature distributed execution and
ecosystem for a domain-owned lifecycle, checkpoint/cancellation semantics, and
operator-visible task management.

## Considered options

- Keep Celery as the permanent execution backend.
- Run Celery and the internal executor as long-term dual backends.
- Build and validate the internal system, then hard-switch once and remove
  Celery after all hard gates pass (chosen).

## Consequences

Running Task Runs are never transferred between executors. At hard cutover,
queued Celery work is withdrawn and active Celery work is terminated and
recorded as `failed/migration_interrupted`; an administrator must inspect the
checkpoint and manually create a new internal retry Run. If the cutover fails,
the new deployment stops future plans and is repaired or replaced as a whole;
it does not fall back to Celery at runtime.
