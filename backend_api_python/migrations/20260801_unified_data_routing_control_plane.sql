-- Unified external-data routing control plane. Additive and safe to apply repeatedly.

CREATE TABLE IF NOT EXISTS qd_data_adapter_types (
    adapter_key VARCHAR(120) PRIMARY KEY,
    adapter_version VARCHAR(64) NOT NULL,
    public_name VARCHAR(160) NOT NULL,
    config_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    credential_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    registration_status VARCHAR(24) NOT NULL DEFAULT 'registered'
        CHECK (registration_status IN ('registered', 'tombstoned')),
    code_fingerprint VARCHAR(64) NOT NULL,
    tombstoned_at TIMESTAMPTZ,
    materialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (adapter_key ~ '^[a-z][a-z0-9_]{1,119}$'),
    CHECK (octet_length(config_schema::text) <= 65536),
    CHECK (octet_length(credential_schema::text) <= 65536),
    CHECK ((registration_status = 'tombstoned') = (tombstoned_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS qd_data_capabilities (
    capability_key VARCHAR(120) PRIMARY KEY,
    capability_version VARCHAR(64) NOT NULL,
    public_name VARCHAR(160) NOT NULL,
    semantic_family VARCHAR(80) NOT NULL,
    market VARCHAR(40) NOT NULL DEFAULT '',
    normalized_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    allowed_constraints JSONB NOT NULL DEFAULT '[]'::jsonb,
    hard_quality_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    cache_key_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    registration_status VARCHAR(24) NOT NULL DEFAULT 'registered'
        CHECK (registration_status IN ('registered', 'tombstoned')),
    code_fingerprint VARCHAR(64) NOT NULL,
    tombstoned_at TIMESTAMPTZ,
    materialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (capability_key ~ '^[a-z][a-z0-9_]{1,119}$'),
    CHECK (octet_length(normalized_contract::text) <= 65536),
    CHECK (octet_length(allowed_constraints::text) <= 32768),
    CHECK (octet_length(hard_quality_contract::text) <= 65536),
    CHECK (octet_length(cache_key_contract::text) <= 32768),
    CHECK ((registration_status = 'tombstoned') = (tombstoned_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS qd_provider_instances (
    id BIGSERIAL PRIMARY KEY,
    instance_key VARCHAR(120) NOT NULL UNIQUE,
    adapter_key VARCHAR(120) NOT NULL REFERENCES qd_data_adapter_types(adapter_key),
    display_name VARCHAR(160) NOT NULL,
    lifecycle_status VARCHAR(24) NOT NULL DEFAULT 'draft' CHECK (lifecycle_status IN (
        'draft', 'validation_failed', 'active', 'draining', 'disabled', 'migration_required', 'retired'
    )),
    provider_account_identity VARCHAR(255),
    account_identity_verified_at TIMESTAMPTZ,
    config_schema_version VARCHAR(64) NOT NULL,
    non_secret_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    config_version INTEGER NOT NULL DEFAULT 1 CHECK (config_version > 0),
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    activated_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (instance_key ~ '^[a-z][a-z0-9_]{1,119}$'),
    CHECK (octet_length(non_secret_config::text) <= 65536),
    CHECK (provider_account_identity IS NULL OR length(provider_account_identity) BETWEEN 1 AND 255),
    CHECK (lifecycle_status <> 'retired' OR retired_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_instances_nonretired_account
    ON qd_provider_instances(adapter_key, provider_account_identity)
    WHERE provider_account_identity IS NOT NULL AND lifecycle_status <> 'retired';
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_instances_single_unidentified_active
    ON qd_provider_instances(adapter_key)
    WHERE provider_account_identity IS NULL AND lifecycle_status IN ('active', 'draining');
CREATE INDEX IF NOT EXISTS idx_provider_instances_lifecycle
    ON qd_provider_instances(lifecycle_status, adapter_key, id);

CREATE TABLE IF NOT EXISTS qd_provider_credentials (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    credential_schema_version VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'active', 'destroyed')),
    encryption_key_id VARCHAR(120) NOT NULL,
    ciphertext TEXT NOT NULL,
    secret_comparison_tag VARCHAR(128) NOT NULL,
    validation_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMPTZ,
    destroyed_at TIMESTAMPTZ,
    UNIQUE (instance_id, credential_version),
    CHECK (octet_length(validation_summary::text) <= 32768),
    CHECK ((status IN ('pending', 'active') AND length(ciphertext) > 0 AND destroyed_at IS NULL)
        OR (status = 'destroyed' AND ciphertext = '' AND destroyed_at IS NOT NULL)),
    CHECK (status <> 'active' OR activated_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_credentials_one_active
    ON qd_provider_credentials(instance_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_credentials_one_pending
    ON qd_provider_credentials(instance_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_credentials_secret_reuse
    ON qd_provider_credentials(secret_comparison_tag);
CREATE INDEX IF NOT EXISTS idx_provider_credentials_key_rotation
    ON qd_provider_credentials(encryption_key_id, status, id)
    WHERE status IN ('active', 'pending');

CREATE TABLE IF NOT EXISTS qd_provider_instance_capabilities (
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE CASCADE,
    capability_key VARCHAR(120) NOT NULL REFERENCES qd_data_capabilities(capability_key),
    eligibility_status VARCHAR(24) NOT NULL DEFAULT 'unverified' CHECK (eligibility_status IN (
        'unverified', 'eligible', 'ineligible', 'disabled', 'migration_required'
    )),
    adapter_version VARCHAR(64) NOT NULL,
    capability_version VARCHAR(64) NOT NULL,
    verification_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_verified_at TIMESTAMPTZ,
    next_verification_at TIMESTAMPTZ,
    disabled_reason VARCHAR(1000) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instance_id, capability_key),
    CHECK (octet_length(verification_evidence::text) <= 65536),
    CHECK (eligibility_status <> 'eligible' OR last_verified_at IS NOT NULL),
    CHECK (eligibility_status <> 'disabled' OR length(disabled_reason) > 0)
);
CREATE INDEX IF NOT EXISTS idx_instance_capabilities_route
    ON qd_provider_instance_capabilities(capability_key, eligibility_status, instance_id);
CREATE INDEX IF NOT EXISTS idx_instance_capabilities_reverify
    ON qd_provider_instance_capabilities(next_verification_at, instance_id)
    WHERE eligibility_status IN ('eligible', 'ineligible', 'migration_required');

CREATE TABLE IF NOT EXISTS qd_provider_quota_states (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE CASCADE,
    capability_key VARCHAR(120) REFERENCES qd_data_capabilities(capability_key),
    bucket_key VARCHAR(120) NOT NULL,
    unit VARCHAR(40) NOT NULL,
    configured_limit NUMERIC(28, 8),
    observed_limit NUMERIC(28, 8),
    consumed NUMERIC(28, 8) NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    reserved NUMERIC(28, 8) NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    reset_at TIMESTAMPTZ,
    observation_source VARCHAR(40) NOT NULL DEFAULT 'unknown',
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (configured_limit IS NULL OR configured_limit >= 0),
    CHECK (observed_limit IS NULL OR observed_limit >= 0),
    CHECK (octet_length(metadata::text) <= 32768)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_quota_scope_bucket
    ON qd_provider_quota_states(instance_id, COALESCE(capability_key, ''), bucket_key);
CREATE INDEX IF NOT EXISTS idx_provider_quota_reset
    ON qd_provider_quota_states(reset_at, instance_id, id);

CREATE TABLE IF NOT EXISTS qd_provider_quota_reservations (
    reservation_id VARCHAR(64) PRIMARY KEY,
    quota_state_id BIGINT NOT NULL REFERENCES qd_provider_quota_states(id) ON DELETE CASCADE,
    holder_id VARCHAR(160) NOT NULL,
    amount NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'consumed', 'released', 'expired')),
    purpose VARCHAR(120) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK ((status = 'reserved' AND completed_at IS NULL) OR status <> 'reserved')
);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_active
    ON qd_provider_quota_reservations(quota_state_id, expires_at, reservation_id)
    WHERE status = 'reserved';
CREATE INDEX IF NOT EXISTS idx_quota_reservations_holder
    ON qd_provider_quota_reservations(holder_id, status, expires_at);

CREATE TABLE IF NOT EXISTS qd_provider_health_states (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE CASCADE,
    capability_key VARCHAR(120) REFERENCES qd_data_capabilities(capability_key),
    health_status VARCHAR(20) NOT NULL DEFAULT 'unknown'
        CHECK (health_status IN ('unknown', 'healthy', 'degraded', 'unhealthy', 'quarantined')),
    circuit_state VARCHAR(20) NOT NULL DEFAULT 'closed'
        CHECK (circuit_state IN ('closed', 'open', 'probe_pending', 'half_open')),
    circuit_reason VARCHAR(1000) NOT NULL DEFAULT '',
    circuit_opened_at TIMESTAMPTZ,
    circuit_until TIMESTAMPTZ,
    quarantine_reason VARCHAR(1000) NOT NULL DEFAULT '',
    quarantined_at TIMESTAMPTZ,
    failure_window JSONB NOT NULL DEFAULT '{}'::jsonb,
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version > 0),
    last_evidence_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(failure_window::text) <= 32768),
    CHECK (health_status <> 'quarantined' OR (quarantined_at IS NOT NULL AND length(quarantine_reason) > 0)),
    CHECK (circuit_state = 'closed' OR length(circuit_reason) > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_health_scope
    ON qd_provider_health_states(instance_id, COALESCE(capability_key, ''));
CREATE INDEX IF NOT EXISTS idx_provider_health_circuit
    ON qd_provider_health_states(circuit_state, circuit_until, instance_id);

CREATE TABLE IF NOT EXISTS qd_provider_health_evidence (
    id BIGSERIAL PRIMARY KEY,
    health_state_id BIGINT NOT NULL REFERENCES qd_provider_health_states(id) ON DELETE CASCADE,
    routed_request_id VARCHAR(64),
    evidence_kind VARCHAR(40) NOT NULL,
    permanence VARCHAR(16) NOT NULL CHECK (permanence IN ('success', 'transient', 'permanent', 'unknown')),
    sanitized_summary VARCHAR(2000) NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(metadata::text) <= 32768)
);
CREATE INDEX IF NOT EXISTS idx_health_evidence_scope_time
    ON qd_provider_health_evidence(health_state_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_health_evidence_retention
    ON qd_provider_health_evidence(occurred_at, id);

CREATE TABLE IF NOT EXISTS qd_data_routing_policies (
    id BIGSERIAL PRIMARY KEY,
    capability_key VARCHAR(120) NOT NULL UNIQUE REFERENCES qd_data_capabilities(capability_key),
    policy_version BIGINT NOT NULL DEFAULT 1 CHECK (policy_version > 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    disabled_reason VARCHAR(1000) NOT NULL DEFAULT '',
    effective_revision_id BIGINT,
    draft_revision_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (enabled OR length(disabled_reason) > 0)
);

CREATE TABLE IF NOT EXISTS qd_data_routing_revisions (
    id BIGSERIAL PRIMARY KEY,
    policy_id BIGINT NOT NULL REFERENCES qd_data_routing_policies(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'superseded', 'discarded')),
    based_on_revision_id BIGINT REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT,
    quality_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    impact_preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    change_reason VARCHAR(1000) NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    published_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    UNIQUE (policy_id, revision_number),
    CHECK (octet_length(quality_profile::text) <= 32768),
    CHECK (octet_length(impact_preview::text) <= 65536),
    CHECK (status NOT IN ('published', 'superseded') OR (published_at IS NOT NULL AND length(change_reason) > 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_revisions_one_draft
    ON qd_data_routing_revisions(policy_id) WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_routing_revisions_history
    ON qd_data_routing_revisions(policy_id, revision_number DESC, id DESC);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_routing_policy_effective_revision_fk') THEN
        ALTER TABLE qd_data_routing_policies ADD CONSTRAINT qd_routing_policy_effective_revision_fk
            FOREIGN KEY (effective_revision_id) REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_routing_policy_draft_revision_fk') THEN
        ALTER TABLE qd_data_routing_policies ADD CONSTRAINT qd_routing_policy_draft_revision_fk
            FOREIGN KEY (draft_revision_id) REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qd_data_routing_entries (
    id BIGSERIAL PRIMARY KEY,
    revision_id BIGINT NOT NULL REFERENCES qd_data_routing_revisions(id) ON DELETE CASCADE,
    position SMALLINT NOT NULL CHECK (position > 0),
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    eligibility_requirements JSONB NOT NULL DEFAULT '{}'::jsonb,
    stricter_quality_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (revision_id, position),
    UNIQUE (revision_id, instance_id),
    CHECK (octet_length(eligibility_requirements::text) <= 32768),
    CHECK (octet_length(stricter_quality_profile::text) <= 32768)
);
CREATE INDEX IF NOT EXISTS idx_routing_entries_instance
    ON qd_data_routing_entries(instance_id, revision_id);

CREATE TABLE IF NOT EXISTS qd_routed_data_requests (
    routed_request_id VARCHAR(64) PRIMARY KEY,
    capability_key VARCHAR(120) NOT NULL REFERENCES qd_data_capabilities(capability_key),
    policy_revision_id BIGINT REFERENCES qd_data_routing_revisions(id) ON DELETE RESTRICT,
    calling_feature VARCHAR(160) NOT NULL,
    request_mode VARCHAR(24) NOT NULL CHECK (request_mode IN ('interactive', 'background', 'backtest')),
    subject_summary VARCHAR(512) NOT NULL DEFAULT '',
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    deadline_at TIMESTAMPTZ,
    final_outcome VARCHAR(32) NOT NULL CHECK (final_outcome IN (
        'fresh_cache', 'provider', 'stale_cache', 'capability_disabled', 'failed'
    )),
    selected_instance_id BIGINT REFERENCES qd_provider_instances(id) ON DELETE SET NULL,
    selected_cache_key_hash VARCHAR(64) NOT NULL DEFAULT '',
    provider_public_name VARCHAR(160) NOT NULL DEFAULT '',
    acquired_at TIMESTAMPTZ,
    freshness VARCHAR(16) NOT NULL DEFAULT 'none' CHECK (freshness IN ('fresh', 'stale', 'none')),
    quality_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    explanation JSONB NOT NULL DEFAULT '{}'::jsonb,
    observability_degraded BOOLEAN NOT NULL DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(constraints::text) <= 32768),
    CHECK (octet_length(quality_warnings::text) <= 32768),
    CHECK (octet_length(explanation::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_routed_requests_capability_time
    ON qd_routed_data_requests(capability_key, created_at DESC, routed_request_id);
CREATE INDEX IF NOT EXISTS idx_routed_requests_outcome_retention
    ON qd_routed_data_requests(final_outcome, created_at, routed_request_id);
CREATE INDEX IF NOT EXISTS idx_routed_requests_instance_time
    ON qd_routed_data_requests(selected_instance_id, created_at DESC, routed_request_id);

ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS routed_request_id VARCHAR(64);
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS provider_instance_id BIGINT;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS policy_revision_id BIGINT;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS attempt_order SMALLINT;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS skip_reason VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS retry_summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE qd_external_data_request_logs ADD COLUMN IF NOT EXISTS quality_outcome JSONB NOT NULL DEFAULT '{}'::jsonb;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_routed_request_fk') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_routed_request_fk
            FOREIGN KEY (routed_request_id) REFERENCES qd_routed_data_requests(routed_request_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_provider_instance_fk') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_provider_instance_fk
            FOREIGN KEY (provider_instance_id) REFERENCES qd_provider_instances(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_policy_revision_fk') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_policy_revision_fk
            FOREIGN KEY (policy_revision_id) REFERENCES qd_data_routing_revisions(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_attempt_order_check') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_attempt_order_check
            CHECK (attempt_order IS NULL OR attempt_order > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_retry_summary_size_check') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_retry_summary_size_check
            CHECK (octet_length(retry_summary::text) <= 32768);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'qd_external_logs_quality_outcome_size_check') THEN
        ALTER TABLE qd_external_data_request_logs ADD CONSTRAINT qd_external_logs_quality_outcome_size_check
            CHECK (octet_length(quality_outcome::text) <= 32768);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_external_logs_routed_attempt
    ON qd_external_data_request_logs(routed_request_id, attempt_order, id);
CREATE INDEX IF NOT EXISTS idx_external_logs_instance_time
    ON qd_external_data_request_logs(provider_instance_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_external_logs_revision_time
    ON qd_external_data_request_logs(policy_revision_id, occurred_at DESC, id DESC);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'qd_provider_health_evidence_routed_request_id_fkey'
          AND conrelid = 'qd_provider_health_evidence'::regclass
    ) THEN
        ALTER TABLE qd_provider_health_evidence
            ADD CONSTRAINT qd_provider_health_evidence_routed_request_id_fkey
            FOREIGN KEY (routed_request_id) REFERENCES qd_routed_data_requests(routed_request_id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS qd_data_source_audit (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    action VARCHAR(80) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(120) NOT NULL,
    reason VARCHAR(1000) NOT NULL,
    before_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id VARCHAR(128) NOT NULL,
    outcome VARCHAR(24) NOT NULL CHECK (outcome IN ('succeeded', 'rejected', 'failed')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(before_summary::text) <= 65536),
    CHECK (octet_length(after_summary::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_data_source_audit_target_time
    ON qd_data_source_audit(target_type, target_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_data_source_audit_actor_time
    ON qd_data_source_audit(actor_user_id, occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS qd_data_source_step_up_proofs (
    proof_hash VARCHAR(64) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
    verification_method VARCHAR(16) NOT NULL CHECK (verification_method IN ('password', 'mfa')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_data_source_step_up_user_expiry
    ON qd_data_source_step_up_proofs(user_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_source_step_up_expiry
    ON qd_data_source_step_up_proofs(expires_at, proof_hash);

CREATE TABLE IF NOT EXISTS qd_provider_diagnostics (
    diagnostic_id VARCHAR(64) PRIMARY KEY,
    instance_id BIGINT NOT NULL REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    capability_key VARCHAR(120) NOT NULL REFERENCES qd_data_capabilities(capability_key),
    requested_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    sanitized_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    quota_reservation_id VARCHAR(64) REFERENCES qd_provider_quota_reservations(reservation_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK (octet_length(sanitized_result::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_provider_diagnostics_instance_time
    ON qd_provider_diagnostics(instance_id, capability_key, created_at DESC, diagnostic_id);

CREATE TABLE IF NOT EXISTS qd_provider_recovery_probes (
    probe_id VARCHAR(64) PRIMARY KEY,
    health_state_id BIGINT NOT NULL REFERENCES qd_provider_health_states(id) ON DELETE CASCADE,
    requested_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    trigger VARCHAR(24) NOT NULL CHECK (trigger IN ('scheduled', 'administrator', 'configuration_change')),
    status VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sanitized_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CHECK (octet_length(sanitized_result::text) <= 65536)
);
CREATE INDEX IF NOT EXISTS idx_recovery_probes_due
    ON qd_provider_recovery_probes(status, not_before, probe_id);
CREATE INDEX IF NOT EXISTS idx_recovery_probes_health_time
    ON qd_provider_recovery_probes(health_state_id, created_at DESC, probe_id);

CREATE TABLE IF NOT EXISTS qd_data_routing_cutovers (
    id BIGSERIAL PRIMARY KEY,
    singleton_key SMALLINT NOT NULL DEFAULT 1 CHECK (singleton_key = 1),
    target_application_version VARCHAR(64) NOT NULL UNIQUE,
    status VARCHAR(24) NOT NULL DEFAULT 'preflight'
        CHECK (status IN ('preflight', 'maintenance', 'activated', 'aborted', 'failed')),
    requested_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    activated_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    reason VARCHAR(1000) NOT NULL,
    inventory_digest VARCHAR(64) NOT NULL,
    gate_result_digest VARCHAR(64) NOT NULL DEFAULT '',
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (status <> 'activated' OR (activated_at IS NOT NULL AND activated_by IS NOT NULL AND length(gate_result_digest) = 64))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_data_routing_one_activated_cutover
    ON qd_data_routing_cutovers(singleton_key) WHERE status = 'activated';
CREATE INDEX IF NOT EXISTS idx_data_routing_cutovers_status_time
    ON qd_data_routing_cutovers(status, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS qd_data_routing_cutover_gates (
    id BIGSERIAL PRIMARY KEY,
    cutover_id BIGINT NOT NULL REFERENCES qd_data_routing_cutovers(id) ON DELETE CASCADE,
    gate_key VARCHAR(80) NOT NULL,
    mandatory BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'passed', 'failed')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at TIMESTAMPTZ,
    UNIQUE (cutover_id, gate_key),
    CHECK (octet_length(evidence::text) <= 65536),
    CHECK (status = 'pending' OR evaluated_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_cutover_gates_blocking
    ON qd_data_routing_cutover_gates(cutover_id, mandatory, status, gate_key);

CREATE TABLE IF NOT EXISTS qd_data_source_legacy_imports (
    adapter_key VARCHAR(80) PRIMARY KEY REFERENCES qd_data_adapter_types(adapter_key),
    instance_id BIGINT UNIQUE REFERENCES qd_provider_instances(id) ON DELETE RESTRICT,
    status VARCHAR(24) NOT NULL DEFAULT 'not_imported' CHECK (status IN ('not_imported','imported','failed')),
    source_names JSONB NOT NULL DEFAULT '[]'::jsonb,
    imported_by INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
    imported_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (octet_length(source_names::text) <= 8192)
);
