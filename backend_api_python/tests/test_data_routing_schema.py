from __future__ import annotations

from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION = (MIGRATIONS / "20260801_unified_data_routing_control_plane.sql").read_text(encoding="utf-8")
INIT_SCHEMA = (MIGRATIONS / "init.sql").read_text(encoding="utf-8")
LEGACY_LOG_WRITER = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "external_data_request_logs.py"
).read_text(encoding="utf-8")


DATA_ROUTING_TABLES = (
    "qd_data_adapter_types",
    "qd_data_capabilities",
    "qd_provider_instances",
    "qd_provider_credentials",
    "qd_provider_instance_capabilities",
    "qd_provider_quota_states",
    "qd_provider_quota_reservations",
    "qd_provider_health_states",
    "qd_provider_health_evidence",
    "qd_data_routing_policies",
    "qd_data_routing_revisions",
    "qd_data_routing_entries",
    "qd_routed_data_requests",
    "qd_data_source_audit",
    "qd_provider_diagnostics",
    "qd_provider_recovery_probes",
    "qd_data_routing_cutovers",
    "qd_data_routing_cutover_gates",
)


def test_all_data_routing_control_plane_tables_are_in_migration_and_bootstrap_schema():
    for table in DATA_ROUTING_TABLES:
        declaration = f"CREATE TABLE IF NOT EXISTS {table}"
        assert declaration in MIGRATION
        assert declaration in INIT_SCHEMA


def test_registry_instance_and_credential_invariants_are_database_enforced():
    assert "CHECK (adapter_key ~ '^[a-z][a-z0-9_]{1,119}$')" in MIGRATION
    assert "CHECK (capability_key ~ '^[a-z][a-z0-9_]{1,119}$')" in MIGRATION
    assert "idx_provider_instances_nonretired_account" in MIGRATION
    assert "idx_provider_instances_single_unidentified_active" in MIGRATION
    assert "idx_provider_credentials_one_active" in MIGRATION
    assert "idx_provider_credentials_one_pending" in MIGRATION
    assert "idx_provider_credentials_secret_reuse" in MIGRATION
    assert "status = 'destroyed' AND ciphertext = ''" in MIGRATION


def test_capability_quota_health_and_policy_scopes_have_required_constraints_and_indexes():
    for name in (
        "idx_instance_capabilities_route",
        "idx_provider_quota_scope_bucket",
        "idx_quota_reservations_active",
        "idx_provider_health_scope",
        "idx_provider_health_circuit",
        "idx_routing_revisions_one_draft",
        "idx_routing_revisions_history",
        "idx_routing_entries_instance",
    ):
        assert name in MIGRATION
    assert "UNIQUE (revision_id, position)" in MIGRATION
    assert "UNIQUE (revision_id, instance_id)" in MIGRATION
    assert "qd_routing_policy_effective_revision_fk" in MIGRATION
    assert "qd_routing_policy_draft_revision_fk" in MIGRATION


def test_routed_summary_attempt_links_and_operations_records_are_declared():
    for column in (
        "routed_request_id VARCHAR(64)",
        "provider_instance_id BIGINT",
        "policy_revision_id BIGINT",
        "attempt_order SMALLINT",
        "skip_reason VARCHAR(120)",
        "retry_summary JSONB",
        "quality_outcome JSONB",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in MIGRATION
    for name in (
        "idx_routed_requests_capability_time",
        "idx_external_logs_routed_attempt",
        "idx_data_source_audit_target_time",
        "idx_provider_diagnostics_instance_time",
        "idx_recovery_probes_due",
        "idx_data_routing_one_activated_cutover",
        "idx_cutover_gates_blocking",
    ):
        assert name in MIGRATION


def test_migration_is_additive_and_keeps_the_legacy_attempt_writer_valid():
    destructive_tokens = ("DROP TABLE", "DROP COLUMN", "TRUNCATE ", "DELETE FROM ")
    upper = MIGRATION.upper()
    assert not any(token in upper for token in destructive_tokens)

    # The old writer intentionally omits all new routing columns. Each new
    # column is nullable or has a default, so this insert remains valid while
    # the additive control-plane schema is deployed ahead of cutover.
    assert "INSERT INTO qd_external_data_request_logs" in LEGACY_LOG_WRITER
    assert "routed_request_id" not in LEGACY_LOG_WRITER.split("VALUES", 1)[0]
    assert "ADD COLUMN IF NOT EXISTS routed_request_id VARCHAR(64);" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS provider_instance_id BIGINT;" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS policy_revision_id BIGINT;" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS attempt_order SMALLINT;" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS skip_reason VARCHAR(120) NOT NULL DEFAULT '';" in MIGRATION


def test_json_metadata_is_bounded_and_operational_history_is_pageable():
    assert MIGRATION.count("octet_length(") >= 20
    for name in (
        "idx_health_evidence_retention",
        "idx_routed_requests_outcome_retention",
        "idx_external_logs_instance_time",
        "idx_data_source_audit_actor_time",
        "idx_provider_diagnostics_instance_time",
    ):
        assert name in MIGRATION
