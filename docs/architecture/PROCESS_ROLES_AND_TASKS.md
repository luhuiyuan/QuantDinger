# Backend Process Roles and Durable Tasks

The production deployment uses one backend image with independent process roles.

| Role | Command | Responsibility |
| --- | --- | --- |
| API | `gunicorn -c gunicorn_config.py run:app` | HTTP, authentication, validation, durable command submission |
| Migration | `python -m app.commands.migrate` | Fail-fast schema application before services start |
| Trading | `python -m app.commands.trading_worker` | Strategy runtimes, pending orders, grid fills, exchange connections |
| Scheduler Worker | `python -m app.commands.scheduler` | Domain Scheduler loops, Task Scheduler, and the bounded Task Run executor |

## Ownership rules

- HTTP processes never start trading or scheduler threads.
- Strategy start, stop, restart, and reconcile requests use `qd_strategy_commands`.
- Trading workers claim commands with PostgreSQL `SKIP LOCKED` semantics.
- A strategy runtime requires a renewable row in `qd_strategy_runtime_leases`.
- Fencing tokens increase when an expired runtime is taken over by another worker.
- Global exchange pollers and scheduler loops use `qd_process_leases` leader ownership.
- Worker health is recorded in `qd_worker_heartbeats`.

## Finite Task boundary

The internal Task Scheduler owns finite jobs that can be checkpointed, retried,
cancelled, and observed independently:

- fast AI analysis;
- agent backtests;
- reflection and AI calibration;
- market catalog synchronization;
- runtime metadata cleanup;
- market history, quote refresh, fundamental incremental sync, and fundamental backfill.

The Task Scheduler must not own long-lived strategy loops, exchange polling,
broker sessions, or grid runtime state. Those remain in the trading process
because they require renewable ownership, reconciliation, and controlled
shutdown. Each finite Task Run executes in an isolated subprocess while the
parent scheduler-worker owns its database lease and lifecycle.

## Redis separation during migration

During the hard-cutover migration, the cache Redis instance remains separate
from the temporary Celery `redis-jobs` instance. After the internal Task
Scheduler passes all deletion gates, `redis-jobs` is removed; cache Redis stays
in service. Queue state must never share an evictable Redis memory policy.

## Deployment sequence

Docker Compose enforces this order:

1. PostgreSQL and cache Redis become healthy; the temporary `redis-jobs` is only
   present before the hard cutover.
2. The migration process exits successfully.
3. API, trading, and scheduler-worker start; scheduler-worker initializes both
   Domain Scheduler and Task Scheduler ownership.
4. Health checks require fresh trading and scheduler-worker heartbeats.

Use these endpoints for operations:

- `/api/health` for liveness;
- `/api/health/ready` for PostgreSQL, cache Redis, and internal scheduler readiness;
- `/api/health/workers` for trading and scheduler-worker heartbeat summaries.
