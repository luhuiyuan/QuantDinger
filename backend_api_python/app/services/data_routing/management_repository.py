"""Sanitized Data Source Operations queries and fail-closed management changes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable, Mapping

from .errors import DataRoutingError


class DataSourceManagementError(DataRoutingError):
    code = "data_source_management_error"


class PostgresDataSourceManagementRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    def overview(self) -> dict[str, Any]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT
                       (SELECT COUNT(*) FROM qd_provider_instances WHERE lifecycle_status<>'retired') AS instances,
                       (SELECT COUNT(*) FROM qd_provider_instances WHERE lifecycle_status='active') AS active_instances,
                       (SELECT COUNT(*) FROM qd_provider_health_states WHERE circuit_state<>'closed') AS open_circuits,
                       (SELECT COUNT(*) FROM qd_provider_health_states WHERE health_status='quarantined') AS quarantined,
                       (SELECT COUNT(*) FROM qd_data_routing_policies WHERE enabled) AS enabled_policies,
                       (SELECT COUNT(*) FROM qd_data_routing_policies WHERE NOT enabled) AS disabled_policies,
                       (SELECT COUNT(*) FROM qd_provider_credentials WHERE status='pending') AS pending_credentials,
                       (SELECT COUNT(*) FROM qd_provider_instance_capabilities WHERE eligibility_status<>'eligible') AS capability_issues"""
                )
                summary = dict(cur.fetchone() or {})
                cur.execute(
                    """SELECT final_outcome,COUNT(*) AS count FROM qd_routed_data_requests
                       WHERE created_at>=NOW()-INTERVAL '24 hours' GROUP BY final_outcome"""
                )
                summary["routing_outcomes_24h"] = {
                    str(row["final_outcome"]): int(row["count"]) for row in (cur.fetchall() or [])
                }
                cur.execute(
                    """SELECT date_trunc('hour',occurred_at) AS bucket,
                              COUNT(*) FILTER (WHERE attempt_order>1) AS fallback_attempts,
                              COUNT(DISTINCT routed_request_id) AS routed_requests
                       FROM qd_external_data_request_logs
                       WHERE occurred_at>=NOW()-INTERVAL '24 hours' AND routed_request_id IS NOT NULL
                       GROUP BY bucket ORDER BY bucket"""
                )
                summary["fallback_trend_24h"] = [dict(row) for row in (cur.fetchall() or [])]
                cur.execute(
                    """SELECT instance_id,capability_key,bucket_key,unit,configured_limit,observed_limit,
                              consumed,reserved,reset_at,
                              COALESCE(LEAST(configured_limit,observed_limit),configured_limit,observed_limit) AS effective_limit
                       FROM qd_provider_quota_states
                       ORDER BY (consumed+reserved) DESC,instance_id,bucket_key LIMIT 20"""
                )
                summary["quota_pressure"] = [dict(row) for row in (cur.fetchall() or [])]
                cur.execute(
                    """SELECT id AS state_id,instance_id,capability_key,health_status,circuit_state,
                              circuit_reason,circuit_until,last_evidence_at
                       FROM qd_provider_health_states
                       WHERE health_status IN ('degraded','unhealthy','quarantined') OR circuit_state<>'closed'
                       ORDER BY last_evidence_at DESC NULLS LAST,id DESC LIMIT 20"""
                )
                summary["active_incidents"] = [dict(row) for row in (cur.fetchall() or [])]
                return summary
            finally:
                cur.close()

    def list_instances(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT COUNT(*) AS count FROM qd_provider_instances")
                total = int(cur.fetchone()["count"])
                cur.execute(
                    """SELECT i.id,i.instance_key,i.adapter_key,i.display_name,i.lifecycle_status,
                              i.provider_account_identity,i.config_schema_version,i.config_version,
                              i.created_at,i.updated_at,
                              EXISTS(SELECT 1 FROM qd_provider_credentials c WHERE c.instance_id=i.id AND c.status='active') AS credential_configured,
                              COALESCE((SELECT jsonb_object_agg(capability_key,eligibility_status)
                                FROM qd_provider_instance_capabilities pc WHERE pc.instance_id=i.id),'{}'::jsonb) AS capabilities
                       FROM qd_provider_instances i ORDER BY i.id LIMIT %s OFFSET %s""",
                    (bounded, offset),
                )
                return {"items": [dict(row) for row in (cur.fetchall() or [])], "total": total, "limit": bounded, "offset": offset}
            finally:
                cur.close()

    def get_instance(self, instance_id: int) -> dict[str, Any] | None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id,instance_key,adapter_key,display_name,lifecycle_status,
                              provider_account_identity,config_schema_version,non_secret_config,
                              config_version,activated_at,disabled_at,retired_at,created_at,updated_at
                       FROM qd_provider_instances WHERE id=%s""",
                    (int(instance_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                result = dict(row)
                cur.execute(
                    """SELECT capability_key,eligibility_status,verification_evidence,disabled_reason,
                              last_verified_at,next_verification_at FROM qd_provider_instance_capabilities
                       WHERE instance_id=%s ORDER BY capability_key""",
                    (int(instance_id),),
                )
                result["capabilities"] = [dict(item) for item in (cur.fetchall() or [])]
                cur.execute(
                    """SELECT id AS state_id,state_version,capability_key,health_status,circuit_state,circuit_reason,circuit_until,
                              quarantine_reason,last_evidence_at FROM qd_provider_health_states
                       WHERE instance_id=%s ORDER BY capability_key NULLS FIRST""",
                    (int(instance_id),),
                )
                result["health"] = [dict(item) for item in (cur.fetchall() or [])]
                cur.execute(
                    """SELECT capability_key,bucket_key,unit,configured_limit,observed_limit,consumed,reserved,
                              reset_at,observation_source FROM qd_provider_quota_states
                       WHERE instance_id=%s ORDER BY capability_key NULLS FIRST,bucket_key""",
                    (int(instance_id),),
                )
                result["quota"] = [dict(item) for item in (cur.fetchall() or [])]
                cur.execute(
                    """SELECT credential_version,encryption_key_id,created_at,activated_at
                       FROM qd_provider_credentials WHERE instance_id=%s AND status='active'""",
                    (int(instance_id),),
                )
                credential = cur.fetchone()
                result["credential"] = {"configured": bool(credential), **(dict(credential) if credential else {})}
                cur.execute(
                    """SELECT p.capability_key,p.effective_revision_id
                       FROM qd_data_routing_policies p
                       JOIN qd_data_routing_entries e ON e.revision_id=p.effective_revision_id
                       WHERE p.enabled AND e.instance_id=%s ORDER BY p.capability_key""",
                    (int(instance_id),),
                )
                result["retirement_blockers"] = [dict(item) for item in (cur.fetchall() or [])]
                cur.execute(
                    """SELECT e.health_state_id,e.occurred_at,e.evidence_kind,e.permanence,
                              e.sanitized_summary,e.metadata,h.capability_key
                       FROM qd_provider_health_evidence e
                       JOIN qd_provider_health_states h ON h.id=e.health_state_id
                       WHERE h.instance_id=%s ORDER BY e.occurred_at DESC,e.id DESC LIMIT 100""",
                    (int(instance_id),),
                )
                result["health_evidence"] = [dict(item) for item in (cur.fetchall() or [])]
                return result
            finally:
                cur.close()

    def list_policies(self) -> list[dict[str, Any]]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT p.id,p.capability_key,p.policy_version,p.enabled,p.disabled_reason,
                              p.effective_revision_id,p.draft_revision_id,p.updated_at,
                              COALESCE((SELECT jsonb_agg(jsonb_build_object(
                                'position',e.position,'instance_id',e.instance_id,'display_name',i.display_name)
                                ORDER BY e.position) FROM qd_data_routing_entries e
                                JOIN qd_provider_instances i ON i.id=e.instance_id
                                WHERE e.revision_id=p.effective_revision_id),'[]'::jsonb) AS entries
                       FROM qd_data_routing_policies p ORDER BY p.capability_key"""
                )
                return [dict(row) for row in (cur.fetchall() or [])]
            finally:
                cur.close()

    def get_policy(self, capability_key: str) -> dict[str, Any] | None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_data_routing_policies WHERE capability_key=%s", (str(capability_key),))
                policy = cur.fetchone()
                if not policy:
                    return None
                result = dict(policy)
                cur.execute(
                    """SELECT r.id,r.revision_number,r.status AS revision_status,r.created_by,r.created_at,r.published_at,
                              COALESCE(jsonb_agg(jsonb_build_object('position',e.position,'instance_id',e.instance_id,
                                'display_name',i.display_name) ORDER BY e.position)
                                FILTER (WHERE e.id IS NOT NULL),'[]'::jsonb) AS entries
                       FROM qd_data_routing_revisions r
                       LEFT JOIN qd_data_routing_entries e ON e.revision_id=r.id
                       LEFT JOIN qd_provider_instances i ON i.id=e.instance_id
                       WHERE r.policy_id=%s GROUP BY r.id ORDER BY r.revision_number DESC LIMIT 50""",
                    (int(policy["id"]),),
                )
                result["revisions"] = [dict(row) for row in (cur.fetchall() or [])]
                return result
            finally:
                cur.close()

    def get_routed_request(self, routed_request_id: str) -> dict[str, Any] | None:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_routed_data_requests WHERE routed_request_id=%s", (str(routed_request_id),))
                row = cur.fetchone()
                if not row:
                    return None
                result = dict(row)
                cur.execute(
                    """SELECT occurred_at,provider,provider_instance_id,policy_revision_id,attempt_order,
                              result,skip_reason,retry_summary,quality_outcome,error_summary
                       FROM qd_external_data_request_logs WHERE routed_request_id=%s
                       ORDER BY attempt_order,id""",
                    (str(routed_request_id),),
                )
                result["attempts"] = [dict(item) for item in (cur.fetchall() or [])]
                return result
            finally:
                cur.close()

    def quarantine(
        self, *, state_id: int, expected_version: int, until: datetime | None,
        actor_user_id: int, reason: str,
    ) -> None:
        self._health_change(
            state_id=state_id, expected_version=expected_version, actor_user_id=actor_user_id,
            reason=reason, action="provider_health_quarantined",
            update_sql="""health_status='quarantined',circuit_state='open',
              circuit_reason='administrator_quarantine',circuit_opened_at=NOW(),circuit_until=%s,
              quarantine_reason=%s,quarantined_at=NOW()""",
            params=(until, reason),
        )

    def extend_circuit(
        self, *, state_id: int, expected_version: int, until: datetime,
        actor_user_id: int, reason: str,
    ) -> None:
        self._health_change(
            state_id=state_id, expected_version=expected_version, actor_user_id=actor_user_id,
            reason=reason, action="provider_circuit_extended",
            update_sql="circuit_until=GREATEST(COALESCE(circuit_until,%s),%s),circuit_reason=%s",
            params=(until, until, reason), require_open=True,
        )

    def _health_change(self, *, state_id, expected_version, actor_user_id, reason, action, update_sql, params, require_open=False):
        if not str(reason or "").strip():
            raise DataSourceManagementError("Management change reason is required")
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_provider_health_states WHERE id=%s FOR UPDATE", (int(state_id),))
                before = cur.fetchone()
                if not before or int(before["state_version"]) != int(expected_version):
                    raise DataSourceManagementError("Provider health state changed concurrently")
                if require_open and before["circuit_state"] == "closed":
                    raise DataSourceManagementError("Closed circuit cannot be extended")
                cur.execute(
                    f"""UPDATE qd_provider_health_states SET {update_sql},state_version=state_version+1,updated_at=NOW()
                        WHERE id=%s AND state_version=%s RETURNING *""",
                    (*params, int(state_id), int(expected_version)),
                )
                after = cur.fetchone()
                cur.execute(
                    """INSERT INTO qd_data_source_audit
                       (actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
                       VALUES (%s,%s,'provider_health',%s,%s,%s::jsonb,%s::jsonb,%s,'succeeded')""",
                    (
                        actor_user_id, action, str(state_id), str(reason).strip(),
                        json.dumps(dict(before), sort_keys=True, default=str),
                        json.dumps(dict(after), sort_keys=True, default=str), uuid.uuid4().hex,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
