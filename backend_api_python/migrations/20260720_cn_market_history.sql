-- Durable China A-share daily history, corporate actions, quality, and sync state.

CREATE TABLE IF NOT EXISTS qd_cn_daily_bars (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL,
    code VARCHAR(6) NOT NULL,
    exchange VARCHAR(2) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    trade_date DATE NOT NULL,
    open DECIMAL(20, 6) NOT NULL CHECK (open > 0),
    high DECIMAL(20, 6) NOT NULL CHECK (high > 0),
    low DECIMAL(20, 6) NOT NULL CHECK (low > 0),
    close DECIMAL(20, 6) NOT NULL CHECK (close > 0),
    volume DECIMAL(28, 4) NOT NULL DEFAULT 0 CHECK (volume >= 0),
    amount DECIMAL(28, 4) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    provider VARCHAR(32) NOT NULL,
    provider_version VARCHAR(32) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL,
    data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, trade_date, provider),
    CHECK (high >= GREATEST(open, close, low)),
    CHECK (low <= LEAST(open, close, high))
);
CREATE INDEX IF NOT EXISTS idx_cn_daily_bars_range
    ON qd_cn_daily_bars(instrument, trade_date);
CREATE INDEX IF NOT EXISTS idx_cn_daily_bars_provider_range
    ON qd_cn_daily_bars(provider, instrument, trade_date);

CREATE TABLE IF NOT EXISTS qd_cn_instruments (
    instrument VARCHAR(32) PRIMARY KEY,
    code VARCHAR(6) NOT NULL,
    exchange VARCHAR(2) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    name VARCHAR(255) NOT NULL DEFAULT '',
    security_type VARCHAR(24) NOT NULL DEFAULT 'ordinary_share',
    listed_on DATE,
    delisted_on DATE,
    source VARCHAR(32) NOT NULL,
    source_version VARCHAR(32) NOT NULL DEFAULT '',
    content_hash VARCHAR(64) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cn_instruments_code_exchange
    ON qd_cn_instruments(code, exchange);

CREATE TABLE IF NOT EXISTS qd_cn_instrument_classifications (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL REFERENCES qd_cn_instruments(instrument) ON DELETE CASCADE,
    classification VARCHAR(32) NOT NULL
        CHECK (classification IN ('main_board', 'star_board', 'chinext', 'st', 'non_st', 'delisting')),
    effective_start DATE NOT NULL,
    effective_end DATE,
    source VARCHAR(32) NOT NULL,
    source_version VARCHAR(32) NOT NULL DEFAULT '',
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, classification, effective_start, source),
    CHECK (effective_end IS NULL OR effective_end >= effective_start)
);
CREATE INDEX IF NOT EXISTS idx_cn_classifications_lookup
    ON qd_cn_instrument_classifications(instrument, effective_start, effective_end, confirmed);

CREATE TABLE IF NOT EXISTS qd_cn_daily_bar_revisions (
    id BIGSERIAL PRIMARY KEY,
    daily_bar_id BIGINT NOT NULL REFERENCES qd_cn_daily_bars(id) ON DELETE CASCADE,
    instrument VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    provider VARCHAR(32) NOT NULL,
    previous_version INTEGER NOT NULL,
    previous_content_hash VARCHAR(64) NOT NULL,
    previous_payload JSONB NOT NULL,
    replacement_content_hash VARCHAR(64) NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_daily_revisions_bar
    ON qd_cn_daily_bar_revisions(daily_bar_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_corporate_actions (
    id BIGSERIAL PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL,
    code VARCHAR(6) NOT NULL,
    exchange VARCHAR(2) NOT NULL CHECK (exchange IN ('SH', 'SZ')),
    event_date DATE NOT NULL,
    category INTEGER NOT NULL,
    event_name VARCHAR(80) NOT NULL DEFAULT '',
    cash_dividend DECIMAL(20, 8),
    rights_price DECIMAL(20, 8),
    bonus_ratio DECIMAL(20, 10),
    rights_ratio DECIMAL(20, 10),
    consolidation_ratio DECIMAL(20, 10),
    provider VARCHAR(32) NOT NULL,
    provider_version VARCHAR(32) NOT NULL DEFAULT '',
    event_key VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    data_version INTEGER NOT NULL DEFAULT 1 CHECK (data_version > 0),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (instrument, provider, event_key)
);
CREATE INDEX IF NOT EXISTS idx_cn_actions_range
    ON qd_cn_corporate_actions(instrument, event_date);

CREATE TABLE IF NOT EXISTS qd_cn_adjustment_factor_versions (
    factor_version VARCHAR(64) PRIMARY KEY,
    instrument VARCHAR(32) NOT NULL,
    mode VARCHAR(16) NOT NULL CHECK (mode IN ('forward', 'backward')),
    algorithm_version VARCHAR(32) NOT NULL,
    action_data_version VARCHAR(64) NOT NULL,
    anchor_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'invalidated')),
    invalidated_reason TEXT NOT NULL DEFAULT '',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    invalidated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cn_factor_versions_active
    ON qd_cn_adjustment_factor_versions(instrument, mode, generated_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_adjustment_factors (
    instrument VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    mode VARCHAR(16) NOT NULL CHECK (mode IN ('forward', 'backward')),
    factor DECIMAL(28, 14) NOT NULL CHECK (factor > 0),
    factor_version VARCHAR(64) NOT NULL
        REFERENCES qd_cn_adjustment_factor_versions(factor_version) ON DELETE CASCADE,
    algorithm_version VARCHAR(32) NOT NULL,
    anchor_date DATE NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument, trade_date, mode, factor_version)
);
CREATE INDEX IF NOT EXISTS idx_cn_factors_lookup
    ON qd_cn_adjustment_factors(instrument, mode, factor_version, trade_date);

CREATE TABLE IF NOT EXISTS qd_cn_trading_status (
    instrument VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    status VARCHAR(24) NOT NULL CHECK (status IN ('trading', 'suspended', 'not_listed', 'delisted')),
    source VARCHAR(32) NOT NULL,
    source_version VARCHAR(32) NOT NULL DEFAULT '',
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_cn_trading_status_range
    ON qd_cn_trading_status(instrument, trade_date, confirmed);

CREATE TABLE IF NOT EXISTS qd_cn_history_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL UNIQUE,
    parent_run_id VARCHAR(40),
    requested_by INTEGER,
    request_kind VARCHAR(24) NOT NULL DEFAULT 'targeted',
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'paused', 'cancelled')),
    target_start DATE NOT NULL,
    target_end DATE NOT NULL,
    total_symbols INTEGER NOT NULL DEFAULT 0,
    succeeded_symbols INTEGER NOT NULL DEFAULT 0,
    failed_symbols INTEGER NOT NULL DEFAULT 0,
    skipped_symbols INTEGER NOT NULL DEFAULT 0,
    current_instrument VARCHAR(32) NOT NULL DEFAULT '',
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error_code VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (target_end >= target_start)
);
CREATE INDEX IF NOT EXISTS idx_cn_sync_runs_status
    ON qd_cn_history_sync_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_history_sync_targets (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(40) NOT NULL REFERENCES qd_cn_history_sync_runs(run_id) ON DELETE CASCADE,
    instrument VARCHAR(32) NOT NULL,
    target_start DATE NOT NULL,
    target_end DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'paused', 'cancelled', 'skipped')),
    page_offset INTEGER NOT NULL DEFAULT 0,
    checkpoint_date DATE,
    attempts INTEGER NOT NULL DEFAULT 0,
    bars_written INTEGER NOT NULL DEFAULT 0,
    actions_written INTEGER NOT NULL DEFAULT 0,
    last_error_code VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, instrument),
    CHECK (target_end >= target_start)
);
CREATE INDEX IF NOT EXISTS idx_cn_sync_targets_status
    ON qd_cn_history_sync_targets(run_id, status, id);

CREATE TABLE IF NOT EXISTS qd_cn_provider_health (
    provider VARCHAR(32) NOT NULL,
    host VARCHAR(255) NOT NULL,
    healthy BOOLEAN NOT NULL DEFAULT FALSE,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms DECIMAL(12, 3),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error_code VARCHAR(64) NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, host)
);

CREATE TABLE IF NOT EXISTS qd_cn_history_quality_findings (
    id BIGSERIAL PRIMARY KEY,
    fingerprint VARCHAR(64) NOT NULL UNIQUE,
    instrument VARCHAR(32) NOT NULL,
    finding_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'blocking')),
    status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS idx_cn_quality_open
    ON qd_cn_history_quality_findings(instrument, status, severity, start_date);

CREATE TABLE IF NOT EXISTS qd_cn_history_coverage (
    instrument VARCHAR(32) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    adjustment_mode VARCHAR(16) NOT NULL CHECK (adjustment_mode IN ('raw', 'forward', 'backward')),
    first_trade_date DATE,
    last_trade_date DATE,
    expected_sessions INTEGER NOT NULL DEFAULT 0,
    actual_sessions INTEGER NOT NULL DEFAULT 0,
    missing_sessions INTEGER NOT NULL DEFAULT 0,
    blocking_findings INTEGER NOT NULL DEFAULT 0,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    data_version VARCHAR(64) NOT NULL DEFAULT '',
    factor_version VARCHAR(64),
    gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_successful_sync_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument, provider, adjustment_mode)
);
CREATE INDEX IF NOT EXISTS idx_cn_coverage_complete
    ON qd_cn_history_coverage(complete, updated_at DESC);

CREATE TABLE IF NOT EXISTS qd_cn_history_operation_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER,
    action VARCHAR(32) NOT NULL,
    run_id VARCHAR(40),
    request_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_status VARCHAR(20) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cn_history_audit_actor
    ON qd_cn_history_operation_audit(actor_user_id, created_at DESC);
