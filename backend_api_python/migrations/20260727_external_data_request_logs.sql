-- External data provider observability. Safe to apply repeatedly.

CREATE TABLE IF NOT EXISTS qd_external_data_request_logs (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider VARCHAR(64) NOT NULL,
    data_domain VARCHAR(48) NOT NULL,
    operation VARCHAR(96) NOT NULL,
    call_source VARCHAR(96) NOT NULL DEFAULT '',
    subject_summary VARCHAR(512) NOT NULL DEFAULT '',
    fallback_index SMALLINT NOT NULL DEFAULT 0 CHECK (fallback_index >= 0),
    retry_count SMALLINT NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    result VARCHAR(32) NOT NULL CHECK (result IN (
        'success', 'timeout', 'rate_limited', 'provider_error', 'network_error',
        'invalid_response', 'disabled', 'skipped'
    )),
    http_status SMALLINT,
    error_summary VARCHAR(1024) NOT NULL DEFAULT '',
    request_id VARCHAR(128) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_occurred
    ON qd_external_data_request_logs (occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_provider_result_time
    ON qd_external_data_request_logs (provider, result, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_domain_time
    ON qd_external_data_request_logs (data_domain, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_request_logs_retention
    ON qd_external_data_request_logs (result, occurred_at, id);

CREATE TABLE IF NOT EXISTS qd_external_data_cache_stats (
    bucket_date DATE NOT NULL,
    data_domain VARCHAR(48) NOT NULL,
    operation VARCHAR(96) NOT NULL,
    cache_hits BIGINT NOT NULL DEFAULT 0 CHECK (cache_hits >= 0),
    cache_misses BIGINT NOT NULL DEFAULT 0 CHECK (cache_misses >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bucket_date, data_domain, operation)
);

CREATE TABLE IF NOT EXISTS qd_external_data_log_cleanup_runs (
    id BIGSERIAL PRIMARY KEY,
    status VARCHAR(16) NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    deleted_count BIGINT NOT NULL DEFAULT 0 CHECK (deleted_count >= 0),
    error_summary VARCHAR(1024) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_external_log_cleanup_runs_recent
    ON qd_external_data_log_cleanup_runs (started_at DESC, id DESC);
