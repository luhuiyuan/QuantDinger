-- Internal finite Task Scheduler hard-cutover schema.
-- Safe to apply repeatedly on an existing QuantDinger database.

DELETE FROM qd_worker_heartbeats WHERE role NOT IN ('api', 'trading', 'scheduler');
ALTER TABLE qd_worker_heartbeats DROP CONSTRAINT IF EXISTS qd_worker_heartbeats_role_check;
ALTER TABLE qd_worker_heartbeats
    ADD CONSTRAINT qd_worker_heartbeats_role_check
    CHECK (role IN ('api', 'trading', 'scheduler'));

-- =============================================================================
-- Internal finite Task Scheduler control plane
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_task_definitions (
    task_key VARCHAR(120) PRIMARY KEY,
    display_name VARCHAR(255) NOT NULL DEFAULT '',
    definition_version VARCHAR(64) NOT NULL,
    parameter_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
    max_concurrency INTEGER CHECK (max_concurrency IS NULL OR max_concurrency > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE qd_task_definitions
    ADD COLUMN IF NOT EXISTS max_concurrency INTEGER;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'qd_task_definitions_max_concurrency_check'
          AND conrelid = 'qd_task_definitions'::regclass
    ) THEN
        ALTER TABLE qd_task_definitions
            ADD CONSTRAINT qd_task_definitions_max_concurrency_check
            CHECK (max_concurrency IS NULL OR max_concurrency > 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qd_task_schedules (
    id BIGSERIAL PRIMARY KEY,
    task_key VARCHAR(120) NOT NULL REFERENCES qd_task_definitions(task_key),
    cron_expression VARCHAR(120) NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    last_scheduled_at TIMESTAMPTZ,
    next_scheduled_at TIMESTAMPTZ,
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_schedules_due ON qd_task_schedules(enabled, next_scheduled_at);
CREATE INDEX IF NOT EXISTS idx_task_schedules_task ON qd_task_schedules(task_key, enabled);

CREATE TABLE IF NOT EXISTS qd_task_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL UNIQUE,
    task_key VARCHAR(120) NOT NULL REFERENCES qd_task_definitions(task_key),
    definition_version VARCHAR(64) NOT NULL,
    schedule_id BIGINT REFERENCES qd_task_schedules(id) ON DELETE SET NULL,
    owner_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    domain_kind VARCHAR(120) NOT NULL DEFAULT '',
    domain_run_id VARCHAR(120) NOT NULL DEFAULT '',
    exclusivity_key VARCHAR(255) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'retry_wait', 'cancel_requested',
        'succeeded', 'failed', 'cancelled'
    )),
    priority SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt_no INTEGER NOT NULL DEFAULT 0 CHECK (attempt_no >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scheduled_at TIMESTAMPTZ,
    stage VARCHAR(120) NOT NULL DEFAULT '',
    progress_current BIGINT NOT NULL DEFAULT 0 CHECK (progress_current >= 0),
    progress_total BIGINT CHECK (progress_total IS NULL OR progress_total >= 0),
    progress_unit VARCHAR(64) NOT NULL DEFAULT '',
    progress_message VARCHAR(500) NOT NULL DEFAULT '',
    checkpoint_ref VARCHAR(255) NOT NULL DEFAULT '',
    result_status VARCHAR(32) NOT NULL DEFAULT '',
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code VARCHAR(120) NOT NULL DEFAULT '',
    error_summary VARCHAR(2000) NOT NULL DEFAULT '',
    heartbeat_at TIMESTAMPTZ,
    last_progress_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retry_of_run_id VARCHAR(64) REFERENCES qd_task_runs(run_id) ON DELETE SET NULL
);
ALTER TABLE qd_task_runs
    ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_task_runs_queue ON qd_task_runs(status, priority, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_status ON qd_task_runs(task_key, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_owner ON qd_task_runs(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_domain ON qd_task_runs(domain_kind, domain_run_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_heartbeat ON qd_task_runs(status, heartbeat_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_schedule_scheduled
    ON qd_task_runs(schedule_id, scheduled_at)
    WHERE schedule_id IS NOT NULL AND scheduled_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_active_exclusivity
    ON qd_task_runs(exclusivity_key)
    WHERE status IN ('queued', 'running', 'retry_wait', 'cancel_requested');

CREATE TABLE IF NOT EXISTS qd_task_events (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(64) REFERENCES qd_task_runs(run_id) ON DELETE CASCADE,
    schedule_id BIGINT REFERENCES qd_task_schedules(id) ON DELETE CASCADE,
    event_type VARCHAR(80) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'info' CHECK (severity IN ('debug', 'info', 'warning', 'error')),
    stage VARCHAR(120) NOT NULL DEFAULT '',
    message VARCHAR(2000) NOT NULL DEFAULT '',
    error_code VARCHAR(120) NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor VARCHAR(32) NOT NULL DEFAULT 'system',
    correlation_id VARCHAR(255) NOT NULL DEFAULT '',
    external_request_id VARCHAR(120) NOT NULL DEFAULT '',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (run_id IS NOT NULL OR schedule_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_task_events_run_time ON qd_task_events(run_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_task_events_schedule_time ON qd_task_events(schedule_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_task_events_type_time ON qd_task_events(event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS qd_task_leases (
    run_id VARCHAR(64) PRIMARY KEY REFERENCES qd_task_runs(run_id) ON DELETE CASCADE,
    holder_id VARCHAR(255) NOT NULL,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_leases_expiry ON qd_task_leases(lease_expires_at);

CREATE TABLE IF NOT EXISTS qd_task_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(120) NOT NULL,
    reason VARCHAR(1000) NOT NULL DEFAULT '',
    before_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_audit_target_time ON qd_task_audit(target_type, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_audit_actor_time ON qd_task_audit(actor_user_id, created_at DESC);
