---
status: issues_found
depth: deep
files_reviewed: 92
findings:
  critical: 8
  warning: 11
  info: 1
  total: 20
---

# Internal Task Scheduler hard-cutover review

**Reviewed:** 2026-07-31
**Depth:** deep
**Repositories:** `QuantDinger`, `QuantDinger-Vue`
**Files reviewed:** 92 tracked/untracked changed files (84 backend, 8 frontend), plus the directly called scheduler, domain, migration, and observability modules needed for call-chain verification.

## Summary

The implementation has several ship-blocking lifecycle and state-consistency defects. In particular, scheduler leadership can be released while an executor child is still running, linked domain/job records are not finalized when generic Runs are cancelled or forcibly terminated, the market catalog still has a non-Task-Run execution path, and the cutover script has a snapshot/stop race. Monitoring still references the deleted Redis jobs exporter. These defects can produce duplicate side effects, falsely successful Runs, permanently stuck user jobs, or an apparently completed hard cutover with work still executing.

## BLOCKER findings

### BL-01: Scheduler releases leadership while executor work is still alive

**File:** `backend_api_python/app/commands/scheduler.py:64-80,108-114`

**Issue:** The executor loop is a daemon thread, while `execute_one()` runs a non-daemon child process and can take hours. On lease renewal failure the main loop breaks, and shutdown only joins the thread for five seconds before releasing `scheduler-global-services`. The thread may still be inside `execute_one()` and the child may still be renewing a Run lease. A new scheduler can acquire leadership and start another executor, defeating the documented global concurrency of one and allowing domain scheduler ownership to overlap.

**Impact:** Duplicate finite-task side effects and concurrent scheduler leaders during failover/shutdown.

**Fix:** Give the executor an explicit cancellation/stop API, synchronously terminate and join the child, wait for the executor thread to finish (or retain the process lease until it does), and only then release the global lease. Treat failed renewal as a fenced shutdown state rather than immediately breaking the loop.

### BL-02: Non-daemon task children survive a dead scheduler parent

**File:** `backend_api_python/app/services/task_control/executor.py:132-140`

**Issue:** Children are created with `daemon=False` and no parent-death signal/process-group cleanup. If the scheduler is killed (OOM, SIGKILL, process crash), the child can continue provider calls and database writes without a parent to renew/fence its Run. After the 180-second stale scan marks the Run `worker_lost` and releases its exclusivity, an administrator or schedule can start a new Run while the orphan still performs side effects.

**Impact:** Duplicate imports/orders/writes and unsafe retries after worker loss.

**Fix:** Put each child in a killable process group and set a Linux parent-death signal (or equivalent watchdog), and make stale-run handling fence all child writes. Verify the child is dead before freeing the active exclusivity key.

### BL-03: Generic cancellation/forced termination does not finalize linked domain jobs

**File:** `backend_api_python/app/services/task_control/builtin_tasks.py:264-296`; `backend_api_python/app/services/task_control/executor.py:199-262`; `backend_api_python/app/routes/task_management.py:220-239`

**Issue:** `agent_backtest` and `fast_ai_analysis` have no cancellation handler and their handlers do not observe `reporter.is_cancel_requested()`. A queued Run can be cancelled without starting the child; a running Run is killed after the grace period/timeout/lease loss. In all these paths the generic Run is finalized, but `qd_agent_jobs` remains `queued`/`running` and the analysis-memory row remains `processing` (including credits that were charged). The same failure occurs for a worker-lost child because no parent remains to call the domain finalizer.

**Impact:** User-facing jobs remain permanently stuck and billing/refund state is incorrect; retries cannot reliably determine whether work completed.

**Fix:** Define per-task cancellation/termination finalizers. Make queued cancellation invoke the finalizer immediately, make timeout/forced-interruption/worker-loss update the linked domain row and refund exactly once, and make handlers check the reporter at safe checkpoints.

### BL-04: CN market-history cancel leaves the generic Run running and can report success

**File:** `backend_api_python/app/routes/cn_market_history_admin.py:247-253`; `backend_api_python/app/services/task_control/builtin_tasks.py:114-126`

**Issue:** The domain cancel endpoint calls `CNMarketHistorySyncService.cancel_run()` but never calls `_request_task_cancel_by_domain()`. The child then observes a cancelled domain and returns a normal result; the executor sees the generic Run still `running` and transitions it to `succeeded`. Conversely, cancelling the generic Run invokes the cancellation handler, but a queued generic Run has no child and never invokes that handler, leaving the domain row pending.

**Impact:** The two source-of-truth state machines diverge and the UI can show a successful Task Run for cancelled work.

**Fix:** Centralize cancellation by domain reference and update both records in one transaction/command. Make both the domain endpoint and Task Management endpoint idempotently request generic cancellation, including queued Runs.

### BL-05: Fundamental retry cannot resume a disk-cancelled checkpoint

**File:** `backend_api_python/app/services/cn_fundamental_history/service.py:120-125`

**Issue:** Hard-disk protection marks the current target `cancelled`, but `retry_failed()` selects only `failed` and `pending` targets. A Run cancelled at a safe disk boundary therefore has no retry targets and cannot resume from its recorded checkpoint, contrary to the cancellation/retry contract.

**Impact:** Safe cancellation loses the only supported recovery path for a partial backfill.

**Fix:** Include `cancelled` targets when constructing a retry, preserving their checkpoint and excluding already-succeeded targets.

### BL-06: Market catalog still runs outside the internal Task Scheduler

**File:** `backend_api_python/app/startup.py:221-236`; `backend_api_python/app/services/market_catalog_sync.py:103-117,152-165`; `backend_api_python/app/routes/settings.py:1854-1855`

**Issue:** Scheduler startup still calls `start_market_catalog_sync_on_boot()`, which starts a daemon thread directly. The settings endpoint also calls `start_market_catalog_sync('manual')` directly. The new registered `market_catalog_sync` Task Definition is therefore not the sole execution path; startup/manual work has no Task Run, lease, progress, audit, or cancellation and can race the scheduled Task Run.

**Impact:** Finite work bypasses the hard cutover and can execute concurrently or invisibly.

**Fix:** Remove direct startup/thread execution and make startup/manual actions submit a registered Task Run (with idempotency and audit). Keep only the Task Scheduler handler calling `run_market_catalog_sync_inline()`.

### BL-07: Hard-cutover snapshot/stop ordering permits unrecorded work

**File:** `scripts/internal_task_hard_cutover.py:314-339`

**Issue:** The script snapshots Beat/worker/queues, writes the recovery manifest and migration records, and only then stops Beat, revokes tasks, purges queues, and stops the worker. Beat can enqueue a new message after the snapshot; a worker can reserve/start it before revoke. That work is absent from the migration audit and can overwrite the domain status after `record_migration()`, while the final snapshot may be empty only because the worker was stopped. The script also treats a stopped Redis container as zero queues (`inspect_queues()` lines 132-136) and accepts Celery inspect exit code 1 as an empty result (lines 95-102), both fail-open conditions.

**Impact:** Cutover can claim success while work was neither recorded nor safely terminated.

**Fix:** Fail closed if any legacy container/inspect operation is unavailable; stop Beat/new submissions first, take a quiescent snapshot, revoke/terminate and verify workers/queues are empty, then record immutable migration outcomes. Re-snapshot until stable before allowing deployment.

### BL-08: Prometheus still targets the deleted Redis jobs exporter

**File:** `ops/prometheus/prometheus.yml:27-29`; `ops/prometheus/alerts.yml:61-66`

**Issue:** Compose removes `redis-jobs` and `redis-jobs-exporter`, but Prometheus still scrapes `redis-jobs-exporter:9121` and alerts on `up{job=~"redis-cache|redis-jobs"}`. The removed target continuously produces `up == 0` and a false Redis exporter alert, and the scrape error obscures real monitoring after hard cutover.

**Impact:** Production observability is broken immediately after deployment and can trigger persistent false incidents.

**Fix:** Remove the `redis-jobs` scrape job and change the alert to the cache exporter only; validate the rendered Prometheus configuration in the hard-cutover gate.

## HIGH findings

### HI-01: Run terminal transitions do not enforce lease ownership or fencing

**File:** `backend_api_python/app/services/task_control/repository.py:507-558,560-585,725-735`

**Issue:** `transition_run()` and `schedule_retry()` update a Run by ID without checking an unexpired `qd_task_leases` row, holder, or fencing token. The schema increments `fencing_token` on claim, but no executor operation carries or validates it. A stale executor can therefore write terminal state after lease expiry or takeover, and lease release is likewise holder-only without a fence.

**Impact:** Stale workers can overwrite newer lifecycle decisions and defeat worker-loss safety.

**Fix:** Carry the claimed fencing token in `TaskRunRecord`/executor state and require `(run_id, holder_id, fencing_token, lease_expires_at > NOW())` on every heartbeat, event, progress, retry, and terminal transition; reject stale writers.

### HI-02: Mutations and task audit records are not atomic

**File:** `backend_api_python/app/routes/task_management.py:103-123,145-155,189-204,233-238,290-296`

**Issue:** Each route commits the schedule/Run mutation through one repository connection and then writes `qd_task_audit` through another. If the audit insert fails, the operation returns an error after the state change has already committed; if an operator retries, the mutation/audit history no longer describes one operation.

**Impact:** Required administrative audit is silently missing for schedule edits, starts, cancellation, and retries.

**Fix:** Put mutation and audit insert in one repository transaction (or use an outbox written in the same transaction and retry its delivery). Return success only once the audit record is durable.

### HI-03: Agent idempotency is split across non-atomic job and Task Run inserts

**File:** `backend_api_python/app/utils/agent_jobs.py:72-119`; `backend_api_python/app/routes/agent_v1/backtests.py:179-197`

**Issue:** The route performs a read-only idempotency precheck, then `submit_job()` inserts `qd_agent_jobs` and separately inserts the Task Run. Two concurrent requests can both pass the precheck; one then gets a unique-index exception, and a failure after the job insert leaves a failed/stale job without a matching Run. A terminal idempotency replay also relies on the precheck rather than an atomic insert/upsert.

**Impact:** Retries return 500 or leave orphan job rows instead of returning the original job as required.

**Fix:** Insert the job and Task Run under one transaction/advisory lock with `ON CONFLICT ... RETURNING` semantics; return the existing job for every idempotent replay and roll back both rows on failure.

### HI-04: Manual retry wrappers lose the generic retry lineage

**File:** `backend_api_python/app/routes/cn_market_history_admin.py:226-240,473-486`

**Issue:** The CN market-history and fundamental retry endpoints create a new generic Run through `_submit_sync`/`_submit_fundamental_sync` but do not pass `retry_of_run_id`. The domain retry has a parent reference, but `qd_task_runs.retry_of_run_id` remains null.

**Impact:** The Task Management audit/console cannot trace these retries through the generic Run graph.

**Fix:** Resolve the original Task Run by domain reference and pass its `run_id` as `retry_of_run_id`; preserve the relationship in the same transaction as the new domain Run.

### HI-05: Legacy schedule environment variables are silently ignored

**File:** `backend_api_python/app/services/task_control/builtin_schedules.py:23-32`; `backend_api_python/env.example:76-77,100,583,633`; `docker-compose.yml:67`

**Issue:** The new built-in schedules hard-code wall-clock Cron expressions, while deployments still expose `MARKET_CATALOG_SYNC_INTERVAL_SEC`, `AI_CALIBRATION_INTERVAL_SEC`, `CN_HISTORY_DAILY_SYNC_INTERVAL_SEC`, `REFLECTION_WORKER_INTERVAL_SEC`, and `CN_QUOTE_REFRESH_INTERVAL_SEC`. Those values no longer affect scheduling, so existing operator tuning is silently discarded.

**Impact:** Production cadence changes without an explicit migration and operators receive misleading configuration guidance.

**Fix:** Remove obsolete variables and document the database schedule migration, or explicitly use them to materialize initial Cron values with a one-time migration and audit.

### HI-06: Hard-cancel race can convert a successful domain operation into a generic failure

**File:** `backend_api_python/app/services/task_control/executor.py:203-228`; `backend_api_python/app/services/task_control/builtin_tasks.py:200-210`

**Issue:** The executor calls a cancellation handler after observing `cancel_requested`. The CN handlers raise when the domain run became terminal just before the cancellation request. The executor then kills the child and marks the generic Run `failed/cancellation_handler_failed`, even if the domain operation already succeeded.

**Impact:** A normal completion near the cancel boundary is reported as a failed Run and may be retried incorrectly.

**Fix:** Make domain cancellation handlers idempotent and return the terminal domain status; reconcile generic status from that status before forcing failure.

### HI-07: Scheduler readiness can be true while finite scheduling is disabled

**File:** `backend_api_python/app/openapi/routes/health.py:136-152`; `backend_api_python/app/commands/scheduler.py:40-54,94-105`

**Issue:** Readiness only checks that one active definition exists and that a scheduler heartbeat is fresh. The command deliberately keeps the process alive with an empty/failed registration path and records a heartbeat even when `task_scheduler` is `not_ready`. A deployment can therefore be ready while no finite Task Scheduler is scanning.

**Impact:** Traffic is admitted while scheduled maintenance and user jobs silently accumulate.

**Fix:** Record a distinct finite-scheduler readiness state and require all expected definitions/schedules to be synchronized before reporting ready; fail the scheduler process or readiness when registration fails.

### HI-08: Hard-cutover script records domain failure before termination is verified

**File:** `scripts/internal_task_hard_cutover.py:197-240,327-339`

**Issue:** `record_migration()` updates domain rows before revoke/terminate and worker stop. A task that is still executing can overwrite the recorded `migration_interrupted`/`migration_skipped` status. The script does not verify that each revoke succeeded before committing those records.

**Impact:** Migration records do not reliably describe what was actually interrupted.

**Fix:** Quiesce and terminate first, verify task process/queue absence, then commit migration records with observed termination outcomes; use a transaction and explicit failure if any revoke fails.

## MEDIUM findings

### ME-01: Frontend pagination invents totals and exposes a phantom page

**File:** `/home/quantadinger/QuantDinger-Vue/src/views/task-management/index.vue:341-353`; `backend_api_python/app/services/task_control/repository.py:1112-1137`

**Issue:** The API returns items/limit/offset but no total. The page fabricates `total = offset + length + (length === pageSize ? 1 : 0)`, so an exact page-sized final result always shows a next page and an empty request.

**Impact:** Operators see incorrect pagination and can miss that no further Runs exist.

**Fix:** Return a count/has-next cursor from the API and bind Ant Design pagination to that authoritative value.

### ME-02: Ordinary users are shown provider-request links they cannot open

**File:** `/home/quantadinger/QuantDinger-Vue/src/views/task-management/index.vue:27-30,264,460`; `src/config/router.config.js:151-160`

**Issue:** Task event rows render an external-request link for every user, but `/external-data-request-logs` is an admin-only route. A normal user who owns an Agent/fast-analysis Run can click a link and receives a forbidden/hidden page.

**Impact:** The “own task logs” experience exposes a broken action and leaks an assumption that the target log is user-visible.

**Fix:** Render the link only for admins, or provide an owner-scoped external request detail endpoint and route.

### ME-03: Polling stop/start can clear the in-flight guard of a newer refresh

**File:** `/home/quantadinger/QuantDinger-Vue/src/views/task-management/index.vue:286-314`

**Issue:** `stopPolling()` sets `refreshInFlight = false` and invalidates sequences while an existing `refreshActive()` promise may still be pending. If the page is reactivated, a new refresh starts; the old promise's `finally` then sets the shared guard false while the new refresh is still active, allowing overlapping poll requests.

**Impact:** Rapid navigation/activation can produce overlapping requests and stale loading state despite the intended single-flight guard.

**Fix:** Use a generation token owned by each refresh and only clear the guard when the same generation completes; abort requests where supported.

## LOW findings

### LO-01: Generic event fields are not bounded before insertion

**File:** `backend_api_python/app/services/task_control/repository.py:804-849`

**Issue:** `event_type`, `actor`, and `error_code` are inserted into bounded `VARCHAR` columns without truncating `event_type`/validating allowed event names. A malformed or unexpectedly long code-generated event causes an insertion exception and can strand the parent Run in `running`.

**Impact:** Operational logging failures can obscure the original task result.

**Fix:** Validate event types and truncate/reject bounded fields before opening the transaction; make event persistence failure non-fatal to lifecycle finalization.

---

_Reviewed: 2026-07-31_
_Reviewer: gsd-code-reviewer_
_Depth: deep_
