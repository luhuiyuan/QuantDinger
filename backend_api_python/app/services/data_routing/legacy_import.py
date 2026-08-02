"""One-time import of supported legacy external-data credentials."""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any, Mapping, Protocol

from .errors import DataRoutingError


class LegacyCredentialImportError(DataRoutingError):
    code = "legacy_credential_import_failed"


@dataclass(frozen=True)
class LegacyCredentialSpec:
    adapter_key: str
    environment_fields: Mapping[str, str]


@dataclass(frozen=True)
class LegacyCredentialDetection:
    adapter_key: str
    detected: bool
    source_names: tuple[str, ...]
    import_status: str = "not_imported"
    instance_id: int | None = None


SUPPORTED_LEGACY_CREDENTIALS = (
    LegacyCredentialSpec("twelve_data", {"api_key": "TWELVE_DATA_API_KEY"}),
    LegacyCredentialSpec("finnhub", {"api_key": "FINNHUB_API_KEY"}),
    LegacyCredentialSpec("tiingo", {"api_key": "TIINGO_API_KEY"}),
    LegacyCredentialSpec("fred", {"api_key": "FRED_API_KEY"}),
    LegacyCredentialSpec("bls", {"api_key": "BLS_API_KEY"}),
    LegacyCredentialSpec("bea", {"api_key": "BEA_API_KEY"}),
    LegacyCredentialSpec("trading_economics", {"api_key": "TRADING_ECONOMICS_CREDENTIALS"}),
    LegacyCredentialSpec("adanos", {"api_key": "ADANOS_API_KEY"}),
    LegacyCredentialSpec("tavily", {"api_key": "TAVILY_API_KEY"}),
    LegacyCredentialSpec("bing", {"api_key": "SEARCH_BING_API_KEY"}),
)


class LegacyImportRepository(Protocol):
    def import_statuses(self) -> Mapping[str, tuple[str, int | None]]: ...
    def record_import(self, adapter_key: str, instance_id: int, *, actor_user_id: int, source_names: tuple[str, ...], reason: str) -> None: ...
    def record_failure(self, adapter_key: str, instance_id: int | None, *, actor_user_id: int, source_names: tuple[str, ...], reason: str) -> None: ...


class LegacyCredentialDetector:
    def __init__(self, environment: Mapping[str, str], repository: LegacyImportRepository | None = None):
        self.environment = environment
        self.repository = repository

    def detect(self) -> tuple[LegacyCredentialDetection, ...]:
        statuses = self.repository.import_statuses() if self.repository else {}
        output = []
        for spec in SUPPORTED_LEGACY_CREDENTIALS:
            names = tuple(name for name in spec.environment_fields.values() if str(self.environment.get(name) or "").strip())
            status, instance_id = statuses.get(spec.adapter_key, ("not_imported", None))
            output.append(LegacyCredentialDetection(spec.adapter_key, bool(names), names, status, instance_id))
        return tuple(output)

    def read_for_import(self, adapter_key: str) -> tuple[dict[str, str], tuple[str, ...]]:
        spec = next((item for item in SUPPORTED_LEGACY_CREDENTIALS if item.adapter_key == adapter_key), None)
        if spec is None:
            raise LegacyCredentialImportError("Legacy credential source is not supported")
        credentials = {field: str(self.environment.get(name) or "").strip() for field, name in spec.environment_fields.items()}
        credentials = {field: value for field, value in credentials.items() if value}
        if set(credentials) != set(spec.environment_fields):
            raise LegacyCredentialImportError("Required legacy credential fields are not configured")
        return credentials, tuple(spec.environment_fields.values())


class LegacyCredentialImportService:
    def __init__(self, detector: LegacyCredentialDetector, repository: LegacyImportRepository, instance_service: Any, credential_service: Any):
        self.detector = detector
        self.repository = repository
        self.instance_service = instance_service
        self.credential_service = credential_service

    def import_adapter(self, adapter_key: str, *, actor_user_id: int, reason: str) -> LegacyCredentialDetection:
        if not str(reason or "").strip():
            raise LegacyCredentialImportError("Legacy credential import reason is required")
        statuses = self.repository.import_statuses()
        if statuses.get(adapter_key, (None, None))[0] == "imported":
            raise LegacyCredentialImportError("Legacy credential has already been imported")
        credentials, source_names = self.detector.read_for_import(adapter_key)
        instance_id = None
        try:
            instance = self.instance_service.create_draft(
                instance_key=f"legacy-{adapter_key}", adapter_key=adapter_key,
                display_name=f"Imported {adapter_key}", config_schema_version="1",
                non_secret_config={}, actor_user_id=actor_user_id,
            )
            instance_id = instance.instance_id
            self.credential_service.submit_and_validate(
                instance_id, credentials, actor_user_id=actor_user_id, reason=reason,
            )
        except Exception:
            self.repository.record_failure(
                adapter_key, instance_id, actor_user_id=actor_user_id,
                source_names=source_names, reason=reason,
            )
            raise
        self.repository.record_import(
            adapter_key, instance_id, actor_user_id=actor_user_id,
            source_names=source_names, reason=reason,
        )
        return LegacyCredentialDetection(adapter_key, True, source_names, "imported", instance_id)


class PostgresLegacyImportRepository:
    def __init__(self, connection_factory=None):
        if connection_factory is None:
            from app.utils.db_postgres import get_pg_connection
            connection_factory = get_pg_connection
        self.connection_factory = connection_factory

    def import_statuses(self) -> Mapping[str, tuple[str, int | None]]:
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT adapter_key,status,instance_id FROM qd_data_source_legacy_imports")
                return {str(row["adapter_key"]): (str(row["status"]), int(row["instance_id"]) if row.get("instance_id") else None) for row in cur.fetchall()}
            finally:
                cur.close()

    def record_import(self, adapter_key: str, instance_id: int, *, actor_user_id: int, source_names: tuple[str, ...], reason: str) -> None:
        safe_sources = list(source_names)
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_data_source_legacy_imports(adapter_key,instance_id,status,source_names,imported_by,imported_at)
                       VALUES (%s,%s,'imported',%s::jsonb,%s,NOW())
                       ON CONFLICT(adapter_key) DO UPDATE SET instance_id=EXCLUDED.instance_id,status='imported',source_names=EXCLUDED.source_names,imported_by=EXCLUDED.imported_by,imported_at=NOW()""",
                    (adapter_key, instance_id, json.dumps(safe_sources), actor_user_id),
                )
                cur.execute(
                    """INSERT INTO qd_data_source_audit(actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
                       VALUES (%s,'legacy_credential_import','provider_instance',%s,%s,'{}'::jsonb,%s::jsonb,%s,'succeeded')""",
                    (actor_user_id, str(instance_id), reason, json.dumps({"adapter_key": adapter_key, "source_names": safe_sources}), str(uuid.uuid4())),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def record_failure(self, adapter_key: str, instance_id: int | None, *, actor_user_id: int, source_names: tuple[str, ...], reason: str) -> None:
        safe_sources = list(source_names)
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_data_source_legacy_imports(adapter_key,instance_id,status,source_names,imported_by,updated_at)
                       VALUES (%s,%s,'failed',%s::jsonb,%s,NOW())
                       ON CONFLICT(adapter_key) DO UPDATE SET instance_id=EXCLUDED.instance_id,status='failed',source_names=EXCLUDED.source_names,imported_by=EXCLUDED.imported_by,updated_at=NOW()""",
                    (adapter_key, instance_id, json.dumps(safe_sources), actor_user_id),
                )
                cur.execute(
                    """INSERT INTO qd_data_source_audit(actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
                       VALUES (%s,'legacy_credential_import','provider_instance',%s,%s,'{}'::jsonb,%s::jsonb,%s,'failed')""",
                    (actor_user_id, str(instance_id or adapter_key), reason, json.dumps({"adapter_key": adapter_key, "source_names": safe_sources, "status": "failed"}), str(uuid.uuid4())),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
