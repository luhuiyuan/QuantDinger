"""Server-owned cutover evidence collection and non-committing dry runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .gateway import CALLING_FEATURE_CAPABILITIES


class PreflightEvidenceRepository(Protocol):
    def control_plane_evidence(self) -> Mapping[str, Any]: ...


def inventory_zero_bypass_evidence(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "passed": False,
            "reason": "inventory artifact is missing or invalid",
            "error_type": type(exc).__name__,
        }
    blocked = [
        item["path"] for item in manifest.get("entries", [])
        if item.get("scope") == "in_scope"
        and (item.get("migration_status") != "routed" or not item.get("router_entries"))
    ]
    return {"passed": not blocked, "blocked_count": len(blocked), "blocked_paths": blocked[:50]}


def test_matrix_evidence(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"passed": False, "reason": "verified test matrix artifact is missing or invalid"}
    return {
        "passed": payload.get("passed") is True,
        "artifact_version": payload.get("artifact_version"),
        "completed_at": payload.get("completed_at"),
        "failed_groups": list(payload.get("failed_groups") or []),
    }


def background_dry_run_evidence() -> dict[str, Any]:
    from .bootstrap import load_default_data_routing_registry

    registry = load_default_data_routing_registry()
    features = {feature: capability for feature, capability in CALLING_FEATURE_CAPABILITIES.items() if feature.startswith("task.")}
    missing = sorted({capability for capability in features.values() if capability not in registry.capabilities})
    unsupported = sorted(
        capability for capability in set(features.values())
        if not any(capability in adapter.capabilities for adapter in registry.adapters.values())
    )
    return {
        "passed": not missing and not unsupported,
        "mode": "non_committing",
        "calling_features": sorted(features),
        "missing_capabilities": missing,
        "unsupported_capabilities": unsupported,
    }


class CutoverEvidenceCollector:
    def __init__(self, repository: PreflightEvidenceRepository, *, inventory_path: Path | None = None, test_matrix_path: Path | None = None, credential_key_check=None, background_check=None):
        artifact_root = Path(__file__).resolve().parents[3] / "data_routing_artifacts"
        self.repository = repository
        self.inventory_path = inventory_path or artifact_root / "EXTERNAL_DATA_OUTBOUND_INVENTORY.json"
        self.test_matrix_path = test_matrix_path or artifact_root / "DATA_ROUTING_TEST_MATRIX.json"
        self.credential_key_check = credential_key_check or self._credential_key_evidence
        self.background_check = background_check or background_dry_run_evidence

    @staticmethod
    def _credential_key_evidence() -> dict[str, Any]:
        try:
            from app.utils.credential_crypto import load_provider_credential_keyring

            keyring = load_provider_credential_keyring()
            return {"passed": bool(keyring.active_key_id), "active_key_id": keyring.active_key_id}
        except Exception as exc:
            return {"passed": False, "reason": type(exc).__name__}

    def collect(self) -> dict[str, Any]:
        try:
            control_plane = dict(self.repository.control_plane_evidence())
        except Exception as exc:
            unavailable = {"passed": False, "reason": "control plane evidence is unavailable", "error_type": type(exc).__name__}
            control_plane = {
                "capability_preflight": dict(unavailable),
                "healthy_instances": dict(unavailable),
                "effective_policies": dict(unavailable),
                "explicit_disables": dict(unavailable),
            }
        evidence = {
            "inventory_zero_bypass": inventory_zero_bypass_evidence(self.inventory_path),
            "test_matrix": test_matrix_evidence(self.test_matrix_path),
            **control_plane,
            "credential_key_readiness": self.credential_key_check(),
            "background_dry_run": self.background_check(),
        }
        required = (
            "inventory_zero_bypass", "test_matrix", "capability_preflight", "healthy_instances",
            "effective_policies", "explicit_disables", "credential_key_readiness", "background_dry_run",
        )
        blockers = [key for key in required if not bool((evidence.get(key) or {}).get("passed"))]
        evidence["go_no_go_report"] = {"passed": not blockers, "decision": "GO" if not blockers else "NO_GO", "blockers": blockers}
        return evidence


class PostgresPreflightEvidenceRepository:
    def __init__(self, connection_factory=None):
        if connection_factory is None:
            from app.utils.db_postgres import get_pg_connection
            connection_factory = get_pg_connection
        self.connection_factory = connection_factory

    def control_plane_evidence(self) -> Mapping[str, Any]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT
                      (SELECT COUNT(*) FROM qd_data_capabilities WHERE registration_status='registered') AS capability_count,
                      (SELECT COUNT(*) FROM qd_data_capabilities c LEFT JOIN qd_data_routing_policies p USING(capability_key)
                         WHERE c.registration_status='registered' AND p.id IS NULL) AS missing_policy_count,
                      (SELECT COUNT(*) FROM qd_data_routing_policies WHERE enabled AND effective_revision_id IS NULL) AS missing_effective_count,
                      (SELECT COUNT(*) FROM qd_data_routing_policies WHERE NOT enabled AND length(trim(disabled_reason))=0) AS invalid_disable_count,
                      (SELECT COUNT(*) FROM qd_data_routing_policies p JOIN qd_data_routing_entries e ON e.revision_id=p.effective_revision_id
                         JOIN qd_provider_instances i ON i.id=e.instance_id
                         JOIN qd_provider_instance_capabilities ic ON ic.instance_id=i.id AND ic.capability_key=p.capability_key
                         LEFT JOIN qd_provider_health_states h ON h.instance_id=i.id AND (h.capability_key=p.capability_key OR h.capability_key IS NULL)
                         WHERE p.enabled AND (i.lifecycle_status<>'active' OR ic.eligibility_status<>'eligible' OR COALESCE(h.circuit_state,'closed')<>'closed' OR COALESCE(h.health_status,'healthy') IN ('unhealthy','quarantined'))) AS unhealthy_entry_count"""
                )
                row = dict(cur.fetchone())
            finally:
                cur.close()
        return {
            "capability_preflight": {"passed": row["capability_count"] > 0 and row["missing_policy_count"] == 0, "capability_count": row["capability_count"], "missing_policy_count": row["missing_policy_count"]},
            "healthy_instances": {"passed": row["unhealthy_entry_count"] == 0, "unhealthy_entry_count": row["unhealthy_entry_count"]},
            "effective_policies": {"passed": row["missing_effective_count"] == 0, "missing_effective_count": row["missing_effective_count"]},
            "explicit_disables": {"passed": row["invalid_disable_count"] == 0, "invalid_disable_count": row["invalid_disable_count"]},
        }
