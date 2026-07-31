# Internal Task Scheduler hard cutover

This runbook is for the one-time deployment that retires the previous finite-task
runtime. The application has no runtime fallback or compatibility mode. Do not
run the destructive step until every gate has evidence and a restorable database
backup has been recorded.

## 1. Preflight inspection

From the repository root, list active, reserved, scheduled, and queued legacy
work:

```bash
python scripts/internal_task_hard_cutover.py inspect
```

Exit code `2` means work remains and the cutover is blocked. Save the JSON
output with the release evidence. Every queued message must be decoded. A
non-zero `undecodableQueueMessages` count blocks cutover with
`legacy_queue_messages_undecodable`; preserve and investigate the raw Redis
message before retrying.

## 2. Recovery information

Create and verify a database backup before termination. Record its immutable
location or backup identifier. The cutover manifest also records the current Git
commit, dirty-worktree flag, image names and image IDs. Recovery means restoring
the recorded database backup together with the complete recorded legacy images
and Compose definition; there is no per-task runtime fallback.

## 3. Terminate legacy work

The destructive command stops the legacy scheduler, records queued work as
`migration_skipped`, records active work as `migration_interrupted`, revokes the
work, purges the legacy queues, and stops the legacy worker:

```bash
python scripts/internal_task_hard_cutover.py cutover \
  --confirm TERMINATE_LEGACY_TASKS \
  --database-url "$DATABASE_URL" \
  --database-backup-ref "<verified-backup-reference>" \
  --manifest artifacts/internal-task-hard-cutover/recovery-manifest.json
```

The command never creates an internal Task Run. An operator must inspect the
domain checkpoint and explicitly retry a failed or cancelled Run from Task
Management. Interrupted legacy work is not automatically resumed.

If any queued message is undecodable, the command stops before it creates the
recovery manifest, stops Beat, writes migration records, revokes tasks, or
purges queues.

## 4. Hard-gate validation

Prepare a JSON evidence file with every required key set to `true` only after
the corresponding test or drill has passed. Then run:

```bash
python scripts/internal_task_hard_cutover.py gate \
  --evidence artifacts/internal-task-hard-cutover/evidence.json
```

The gate fails if evidence is missing, a value is not exactly `true`, queue
lengths are non-zero, a queue message is undecodable, or
active/reserved/scheduled work remains.

## 5. Deploy and verify

Deploy the backend, scheduler-worker, and frontend sequentially under the
low-memory build policy. Verify `/api/health/ready`, `/api/health/workers`, the
Task Management page, Domain Scheduler health, and `docker compose ps -a`.
There must be no retired worker, scheduler, or task-queue container.

## Failure handling

If any gate or post-deployment check fails, pause future internal schedules and
preserve Run, event, lease, audit, and domain checkpoint records. Fix and
redeploy the complete internal-task version. Do not hand an active internal Run
to the retired runtime and do not fabricate an internal Run for interrupted
legacy work.
