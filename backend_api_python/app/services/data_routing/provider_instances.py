"""Provider Instance lifecycle state machine and repository boundary."""

from __future__ import annotations

import re
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .errors import DataRoutingError


class ProviderInstanceStateError(DataRoutingError):
    code = "provider_instance_invalid_transition"


class ProviderInstanceConflictError(DataRoutingError):
    code = "provider_instance_conflict"


@dataclass(frozen=True, slots=True)
class ProviderInstanceRecord:
    id: int
    instance_key: str
    adapter_key: str
    display_name: str
    lifecycle_status: str
    config_schema_version: str
    non_secret_config: dict[str, Any]
    config_version: int
    activated_at: Any = None
    disabled_at: Any = None
    retired_at: Any = None

    @property
    def ever_activated(self) -> bool:
        return self.activated_at is not None


class ProviderInstanceRepository(Protocol):
    def create_draft(self, *, instance_key: str, adapter_key: str, display_name: str, config_schema_version: str, non_secret_config: dict[str, Any], actor_user_id: int | None) -> ProviderInstanceRecord: ...
    def get(self, instance_id: int) -> ProviderInstanceRecord | None: ...
    def transition(self, *, instance_id: int, expected_config_version: int, from_states: tuple[str, ...], to_state: str, actor_user_id: int | None, reason: str) -> ProviderInstanceRecord: ...
    def eligible_capability_count(self, instance_id: int) -> int: ...
    def has_effective_routing_reference(self, instance_id: int) -> bool: ...
    def has_operational_history(self, instance_id: int) -> bool: ...
    def delete_unused_draft(self, instance_id: int, expected_config_version: int) -> bool: ...


class ProviderInstanceService:
    def __init__(self, repository: ProviderInstanceRepository):
        self.repository = repository

    def create_draft(self, *, instance_key: str, adapter_key: str, display_name: str, config_schema_version: str, non_secret_config: dict[str, Any], actor_user_id: int | None) -> ProviderInstanceRecord:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,119}", str(instance_key or "")):
            raise ProviderInstanceStateError("invalid stable Provider Instance key")
        if not str(display_name or "").strip():
            raise ProviderInstanceStateError("Provider Instance display name is required")
        return self.repository.create_draft(instance_key=instance_key, adapter_key=adapter_key, display_name=display_name.strip(), config_schema_version=config_schema_version, non_secret_config=dict(non_secret_config), actor_user_id=actor_user_id)

    def _load(self, instance_id: int) -> ProviderInstanceRecord:
        record = self.repository.get(instance_id)
        if record is None:
            raise ProviderInstanceStateError("Provider Instance does not exist", details={"instance_id": instance_id})
        return record

    def _transition(self, instance_id: int, *, allowed: tuple[str, ...], target: str, actor_user_id: int | None, reason: str = "") -> ProviderInstanceRecord:
        record = self._load(instance_id)
        if record.lifecycle_status not in allowed:
            raise ProviderInstanceStateError(
                f"cannot transition Provider Instance from {record.lifecycle_status} to {target}",
                details={"instance_id": instance_id},
            )
        return self.repository.transition(instance_id=instance_id, expected_config_version=record.config_version, from_states=allowed, to_state=target, actor_user_id=actor_user_id, reason=reason)

    def record_validation_failure(self, instance_id: int, *, actor_user_id: int | None, reason: str) -> ProviderInstanceRecord:
        if not str(reason or "").strip():
            raise ProviderInstanceStateError("validation failure reason is required")
        return self._transition(instance_id, allowed=("draft", "validation_failed"), target="validation_failed", actor_user_id=actor_user_id, reason=reason)

    def activate(self, instance_id: int, *, actor_user_id: int | None) -> ProviderInstanceRecord:
        if self.repository.eligible_capability_count(instance_id) < 1:
            raise ProviderInstanceStateError("activation requires at least one verified eligible Capability")
        return self._transition(instance_id, allowed=("draft", "validation_failed", "disabled"), target="active", actor_user_id=actor_user_id)

    def begin_draining(self, instance_id: int, *, actor_user_id: int | None, reason: str) -> ProviderInstanceRecord:
        if not str(reason or "").strip():
            raise ProviderInstanceStateError("draining reason is required")
        return self._transition(instance_id, allowed=("active",), target="draining", actor_user_id=actor_user_id, reason=reason)

    def disable(self, instance_id: int, *, actor_user_id: int | None, reason: str) -> ProviderInstanceRecord:
        if not str(reason or "").strip():
            raise ProviderInstanceStateError("disable reason is required")
        return self._transition(instance_id, allowed=("active", "draining"), target="disabled", actor_user_id=actor_user_id, reason=reason)

    def retire(self, instance_id: int, *, actor_user_id: int | None, reason: str) -> ProviderInstanceRecord:
        if not str(reason or "").strip():
            raise ProviderInstanceStateError("retirement reason is required")
        record = self._load(instance_id)
        if self.repository.has_effective_routing_reference(instance_id):
            raise ProviderInstanceStateError("Provider Instance remains in an effective routing revision")
        if record.lifecycle_status not in ("disabled", "validation_failed"):
            raise ProviderInstanceStateError("Provider Instance must be disabled before retirement")
        return self._transition(instance_id, allowed=("disabled", "validation_failed"), target="retired", actor_user_id=actor_user_id, reason=reason)

    def discard_unused_draft(self, instance_id: int) -> None:
        record = self._load(instance_id)
        if record.lifecycle_status not in ("draft", "validation_failed") or record.ever_activated:
            raise ProviderInstanceStateError("only a never-activated draft may be discarded")
        if self.repository.has_effective_routing_reference(instance_id) or self.repository.has_operational_history(instance_id):
            raise ProviderInstanceStateError("Provider Instance with routing or operational history cannot be discarded")
        if not self.repository.delete_unused_draft(instance_id, record.config_version):
            raise ProviderInstanceConflictError("Provider Instance changed concurrently")


class PostgresProviderInstanceRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection
            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    @staticmethod
    def _record(row) -> ProviderInstanceRecord:
        return ProviderInstanceRecord(**dict(row))

    def create_draft(self, **values):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("""INSERT INTO qd_provider_instances
                    (instance_key,adapter_key,display_name,config_schema_version,non_secret_config,created_by,updated_by)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)
                    RETURNING id,instance_key,adapter_key,display_name,lifecycle_status,config_schema_version,
                              non_secret_config,config_version,activated_at,disabled_at,retired_at""",
                    (values["instance_key"], values["adapter_key"], values["display_name"], values["config_schema_version"], __import__("json").dumps(values["non_secret_config"]), values["actor_user_id"], values["actor_user_id"]))
                row = cur.fetchone()
                cur.execute(
                    """INSERT INTO qd_data_source_audit
                       (actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
                       VALUES (%s,'provider_instance_draft_created','provider_instance',%s,
                               'create_provider_instance_draft','{}'::jsonb,%s::jsonb,%s,'succeeded')""",
                    (
                        values["actor_user_id"], str(row["id"]),
                        json.dumps({
                            "instance_key": values["instance_key"],
                            "adapter_key": values["adapter_key"],
                            "display_name": values["display_name"],
                        }, sort_keys=True), uuid.uuid4().hex,
                    ),
                )
                db.commit(); return self._record(row)
            finally: cur.close()

    def get(self, instance_id: int):
        with self.connection_factory() as db:
            cur=db.cursor()
            try:
                cur.execute("""SELECT id,instance_key,adapter_key,display_name,lifecycle_status,config_schema_version,
                    non_secret_config,config_version,activated_at,disabled_at,retired_at FROM qd_provider_instances WHERE id=%s""", (instance_id,))
                row=cur.fetchone(); return self._record(row) if row else None
            finally: cur.close()

    def transition(self, **values):
        timestamp_column = {"active": "activated_at", "disabled": "disabled_at", "retired": "retired_at"}.get(values["to_state"])
        stamp_sql = f", {timestamp_column}=NOW()" if timestamp_column else ""
        with self.connection_factory() as db:
            cur=db.cursor()
            try:
                cur.execute(f"""UPDATE qd_provider_instances SET lifecycle_status=%s, config_version=config_version+1,
                    updated_by=%s, updated_at=NOW(){stamp_sql} WHERE id=%s AND config_version=%s AND lifecycle_status=ANY(%s)
                    RETURNING id,instance_key,adapter_key,display_name,lifecycle_status,config_schema_version,
                              non_secret_config,config_version,activated_at,disabled_at,retired_at""",
                    (values["to_state"], values["actor_user_id"], values["instance_id"], values["expected_config_version"], list(values["from_states"])))
                row=cur.fetchone()
                if not row: db.rollback(); raise ProviderInstanceConflictError("Provider Instance changed concurrently")
                cur.execute(
                    """INSERT INTO qd_data_source_audit
                       (actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
                       VALUES (%s,%s,'provider_instance',%s,%s,%s::jsonb,%s::jsonb,%s,'succeeded')""",
                    (
                        values["actor_user_id"], f"provider_instance_{values['to_state']}", str(values["instance_id"]),
                        str(values.get("reason") or values["to_state"]),
                        json.dumps({"lifecycle_status": values["from_states"]}, sort_keys=True),
                        json.dumps({"lifecycle_status": values["to_state"]}, sort_keys=True), uuid.uuid4().hex,
                    ),
                )
                db.commit(); return self._record(row)
            finally: cur.close()

    def _exists(self, sql: str, instance_id: int) -> bool:
        with self.connection_factory() as db:
            cur=db.cursor()
            try: cur.execute(sql,(instance_id,)); return bool(cur.fetchone())
            finally: cur.close()

    def eligible_capability_count(self, instance_id: int) -> int:
        with self.connection_factory() as db:
            cur=db.cursor()
            try:
                cur.execute("SELECT COUNT(*) AS count FROM qd_provider_instance_capabilities WHERE instance_id=%s AND eligibility_status='eligible'",(instance_id,))
                return int(cur.fetchone()["count"])
            finally: cur.close()

    def has_effective_routing_reference(self, instance_id: int) -> bool:
        return self._exists("""SELECT 1 FROM qd_data_routing_entries e JOIN qd_data_routing_policies p
            ON p.effective_revision_id=e.revision_id WHERE e.instance_id=%s LIMIT 1""", instance_id)

    def has_operational_history(self, instance_id: int) -> bool:
        return self._exists("""SELECT 1 FROM qd_external_data_request_logs WHERE provider_instance_id=%s LIMIT 1""", instance_id)

    def delete_unused_draft(self, instance_id: int, expected_config_version: int) -> bool:
        with self.connection_factory() as db:
            cur=db.cursor()
            try:
                cur.execute("""DELETE FROM qd_provider_instances WHERE id=%s AND config_version=%s
                    AND lifecycle_status IN ('draft','validation_failed') AND activated_at IS NULL
                    AND NOT EXISTS (SELECT 1 FROM qd_data_routing_entries WHERE instance_id=%s)
                    AND NOT EXISTS (SELECT 1 FROM qd_external_data_request_logs WHERE provider_instance_id=%s)""",
                    (instance_id,expected_config_version,instance_id,instance_id))
                changed=cur.rowcount==1; db.commit(); return changed
            finally: cur.close()
