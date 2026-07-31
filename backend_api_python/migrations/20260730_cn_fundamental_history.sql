-- Durable annual A-share fundamental history and quality operations.
CREATE TABLE IF NOT EXISTS qd_cn_fundamental_observations (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    frequency VARCHAR(16) NOT NULL DEFAULT 'annual' CHECK (frequency = 'annual'),
    source VARCHAR(48) NOT NULL,
    source_version VARCHAR(96) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    announcement_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    supersedes_observation_id BIGINT REFERENCES qd_cn_fundamental_observations(id),
    observation_version INTEGER NOT NULL DEFAULT 1 CHECK (observation_version > 0),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, period_end, available_at, source, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_observation_pit ON qd_cn_fundamental_observations(instrument, available_at DESC, period_end DESC);
ALTER TABLE qd_cn_fundamental_observations ADD COLUMN IF NOT EXISTS observation_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_field_values (
    id BIGSERIAL PRIMARY KEY,
    observation_id BIGINT NOT NULL REFERENCES qd_cn_fundamental_observations(id) ON DELETE CASCADE,
    field_code VARCHAR(80) NOT NULL,
    value_numeric DECIMAL(30, 8),
    value_text TEXT,
    unit VARCHAR(32) NOT NULL DEFAULT '',
    mapping_version VARCHAR(48) NOT NULL,
    source_field VARCHAR(120) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (observation_id, field_code)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_fields_lookup ON qd_cn_fundamental_field_values(field_code, observation_id);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_metric_values (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    as_of_period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    metric_code VARCHAR(80) NOT NULL,
    value_numeric DECIMAL(30, 10),
    status VARCHAR(24) NOT NULL CHECK (status IN ('available', 'insufficient', 'invalid')),
    formula_version VARCHAR(48) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, as_of_period_end, available_at, metric_code, formula_version)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_metrics_pit ON qd_cn_fundamental_metric_values(instrument, available_at DESC, metric_code);

CREATE TABLE IF NOT EXISTS qd_cn_industry_classifications (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    taxonomy VARCHAR(48) NOT NULL,
    industry_code VARCHAR(64) NOT NULL,
    industry_name VARCHAR(255) NOT NULL,
    effective_start DATE NOT NULL,
    effective_end DATE,
    source VARCHAR(48) NOT NULL,
    source_version VARCHAR(96) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (instrument, taxonomy, industry_code, effective_start, source)
);
CREATE INDEX IF NOT EXISTS idx_cn_industry_pit ON qd_cn_industry_classifications(instrument, taxonomy, effective_start, effective_end);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_verification_targets (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    trigger_metric VARCHAR(80) NOT NULL,
    trigger_value DECIMAL(30, 10),
    threshold_value DECIMAL(30, 10),
    status VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'warning', 'blocked', 'unavailable')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (instrument, period_end, trigger_metric)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_targets_status ON qd_cn_fundamental_verification_targets(status, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_reconciliations (
    id BIGSERIAL PRIMARY KEY,
    target_id BIGINT NOT NULL REFERENCES qd_cn_fundamental_verification_targets(id) ON DELETE CASCADE,
    field_code VARCHAR(80) NOT NULL,
    primary_value DECIMAL(30, 8), official_value DECIMAL(30, 8),
    difference_pct DECIMAL(12, 6),
    status VARCHAR(16) NOT NULL CHECK (status IN ('matched', 'warning', 'blocking', 'unavailable')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (target_id, field_code)
);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_quality_results (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    available_at DATE NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('passed_pending_verification', 'excluded', 'insufficient', 'pending_verification', 'blocked', 'verified_with_warnings', 'verified')),
    formula_version VARCHAR(48) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, period_end, available_at, formula_version)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_quality_status ON qd_cn_fundamental_quality_results(status, calculated_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_quality_issues (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    period_end DATE,
    issue_code VARCHAR(80) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('warning', 'blocking')),
    status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    source VARCHAR(48) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (instrument, period_end, issue_code, source)
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_issues_open ON qd_cn_fundamental_quality_issues(status, severity, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_coverage (
    instrument VARCHAR(32) PRIMARY KEY REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    first_period_end DATE,
    last_period_end DATE,
    observation_count INTEGER NOT NULL DEFAULT 0,
    complete_year_count INTEGER NOT NULL DEFAULT 0,
    missing_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_available_at DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL UNIQUE,
    requested_by INTEGER,
    request_kind VARCHAR(24) NOT NULL CHECK (request_kind IN ('backfill', 'scheduled', 'retry')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'paused', 'cancelled')),
    total_symbols INTEGER NOT NULL DEFAULT 0, succeeded_symbols INTEGER NOT NULL DEFAULT 0,
    failed_symbols INTEGER NOT NULL DEFAULT 0, skipped_symbols INTEGER NOT NULL DEFAULT 0,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb, result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS qd_cn_fundamental_sync_targets (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL REFERENCES qd_cn_fundamental_sync_runs(run_id) ON DELETE CASCADE,
    instrument VARCHAR(32) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'pending',
    checkpoint_period_end DATE, attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '', started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (run_id, instrument)
);

CREATE TABLE IF NOT EXISTS qd_cn_fundamental_operation_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER,
    action VARCHAR(64) NOT NULL,
    run_id VARCHAR(40),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_fundamental_audit_actor ON qd_cn_fundamental_operation_audit(actor_user_id, created_at DESC);
