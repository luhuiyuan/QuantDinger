"""PostgreSQL persistence for write-only Provider credentials and eligibility."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Mapping, Sequence

from .credentials import (
    CapabilityVerification,
    CredentialInstanceContext,
    CredentialStatus,
    DuplicateProviderCredentialError,
    ProviderAccountConflictError,
    ProviderCredentialError,
    StoredCredentialSecret,
)


_KEY_ROTATION_LOCK_NAME = "provider-credential-key-rotation"


def _is_secret_reuse_violation(exc: Exception) -> bool:
    return getattr(getattr(exc, "diag", None), "constraint_name", None) == "idx_provider_credentials_secret_reuse"


def _evidence(results: Sequence[CapabilityVerification], *, trigger: str = "credential_validation") -> str:
    return json.dumps(
        {
            "trigger": trigger,
            "results": [
                {"capability_key": item.capability_key, "outcome": item.outcome, "evidence": dict(item.evidence)}
                for item in results
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class PostgresProviderCredentialRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection
            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    @contextmanager
    def _connection(self):
        with self.connection_factory() as db:
            sanitized_error = None
            try:
                yield db
            except ProviderCredentialError:
                raise
            except Exception:
                sanitized_error = ProviderCredentialError("Provider credential persistence operation failed")
            if sanitized_error is not None:
                raise sanitized_error

    @staticmethod
    def _secret(row) -> StoredCredentialSecret | None:
        return StoredCredentialSecret(**dict(row)) if row else None

    @staticmethod
    def _audit(cur, *, actor_user_id, action, target_id, reason, before, after, outcome="succeeded") -> None:
        cur.execute(
            """INSERT INTO qd_data_source_audit
               (actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
               VALUES (%s,%s,'provider_instance',%s,%s,%s::jsonb,%s::jsonb,%s,%s)""",
            (
                actor_user_id,
                action,
                str(target_id),
                str(reason),
                json.dumps(before, sort_keys=True),
                json.dumps(after, sort_keys=True),
                uuid.uuid4().hex,
                outcome,
            ),
        )

    def get_instance_context(self, instance_id: int):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id AS instance_id,adapter_key,lifecycle_status,provider_account_identity,non_secret_config
                       FROM qd_provider_instances WHERE id=%s""",
                    (instance_id,),
                )
                row = cur.fetchone()
                return CredentialInstanceContext(**dict(row)) if row else None
            finally:
                cur.close()

    def find_duplicate_secret_tag(self, secret_tag: str):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT instance_id FROM qd_provider_credentials WHERE secret_comparison_tag=%s LIMIT 1", (secret_tag,))
                row = cur.fetchone()
                return int(row["instance_id"]) if row else None
            finally:
                cur.close()

    def stage_pending_credential(self, **values):
        with self._connection() as db:
            cur = db.cursor()
            sanitized_error = None
            try:
                cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext(%s))", (_KEY_ROTATION_LOCK_NAME,))
                cur.execute("SELECT id FROM qd_provider_instances WHERE id=%s AND lifecycle_status<>'retired' FOR UPDATE", (values["instance_id"],))
                if not cur.fetchone():
                    raise ProviderCredentialError("Provider Instance is unavailable")
                cur.execute(
                    """UPDATE qd_provider_credentials SET status='destroyed',ciphertext='',
                       secret_comparison_tag='discarded:' || id::text,destroyed_at=NOW()
                       WHERE instance_id=%s AND status='pending'""",
                    (values["instance_id"],),
                )
                cur.execute("SELECT COALESCE(MAX(credential_version),0)+1 AS next_version FROM qd_provider_credentials WHERE instance_id=%s", (values["instance_id"],))
                version = int(cur.fetchone()["next_version"])
                cur.execute(
                    """INSERT INTO qd_provider_credentials
                       (instance_id,credential_version,credential_schema_version,status,encryption_key_id,ciphertext,
                        secret_comparison_tag,validation_summary,created_by)
                       VALUES (%s,%s,%s,'pending',%s,%s,%s,'{}'::jsonb,%s)
                       RETURNING id AS credential_id,credential_version,credential_schema_version,status,
                                 encryption_key_id,ciphertext""",
                    (
                        values["instance_id"], version, values["schema_version"], values["key_id"],
                        values["ciphertext"], values["secret_tag"], values["actor_user_id"],
                    ),
                )
                row = cur.fetchone()
                self._audit(
                    cur,
                    actor_user_id=values["actor_user_id"],
                    action="provider_credential_submitted",
                    target_id=values["instance_id"],
                    reason=values["reason"],
                    before={"pending": False},
                    after={"pending": True, "credential_version": version, "encryption_key_id": values["key_id"]},
                )
                db.commit()
                return self._secret(row)
            except ProviderCredentialError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                if _is_secret_reuse_violation(exc):
                    sanitized_error = DuplicateProviderCredentialError("Provider credential bundle is already in use")
                else:
                    sanitized_error = ProviderCredentialError("Provider credential persistence operation failed")
            finally:
                cur.close()
            if sanitized_error is not None:
                raise sanitized_error

    def destroy_pending_credential(self, credential_id: int, *, actor_user_id: int | None, reason: str) -> None:
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_credentials SET status='destroyed',ciphertext='',
                       secret_comparison_tag='discarded:' || id::text,destroyed_at=NOW()
                       WHERE id=%s AND status='pending' RETURNING instance_id,credential_version""",
                    (credential_id,),
                )
                row = cur.fetchone()
                if row:
                    self._audit(cur, actor_user_id=actor_user_id, action="provider_credential_pending_destroyed",
                                target_id=row["instance_id"], reason=reason,
                                before={"pending_version": row["credential_version"]}, after={"pending": False})
                db.commit()
            finally:
                cur.close()

    def get_active_credential(self, instance_id: int):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id AS credential_id,credential_version,credential_schema_version,status,encryption_key_id,ciphertext
                       FROM qd_provider_credentials WHERE instance_id=%s AND status='active'""",
                    (instance_id,),
                )
                return self._secret(cur.fetchone())
            finally:
                cur.close()

    def record_pending_validation(self, credential_id: int, results, *, initial_credential: bool) -> None:
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_credentials SET validation_summary=%s::jsonb
                       WHERE id=%s AND status='pending'
                       RETURNING instance_id,credential_schema_version""",
                    (_evidence(results), credential_id),
                )
                row = cur.fetchone()
                if row:
                    for result in results:
                        status = "ineligible" if result.outcome == "ineligible" else "unverified"
                        cur.execute(
                            """INSERT INTO qd_provider_instance_capabilities
                               (instance_id,capability_key,eligibility_status,adapter_version,capability_version,
                                verification_evidence,last_verified_at,next_verification_at)
                               VALUES (%s,%s,%s,%s,%s,%s::jsonb,NULL,NOW()+INTERVAL '7 days')
                               ON CONFLICT (instance_id,capability_key) DO UPDATE SET
                                eligibility_status=CASE
                                  WHEN qd_provider_instance_capabilities.eligibility_status='disabled' THEN 'disabled'
                                  ELSE EXCLUDED.eligibility_status
                                END,
                                adapter_version=EXCLUDED.adapter_version,
                                capability_version=EXCLUDED.capability_version,
                                verification_evidence=EXCLUDED.verification_evidence,
                                last_verified_at=NULL,
                                next_verification_at=EXCLUDED.next_verification_at,
                                updated_at=NOW()""",
                            (
                                row["instance_id"], result.capability_key, status,
                                row["credential_schema_version"],
                                str(result.evidence.get("capability_version") or ""),
                                json.dumps(dict(result.evidence)),
                            ),
                        )
                    if initial_credential:
                        cur.execute("UPDATE qd_provider_instances SET lifecycle_status='validation_failed',updated_at=NOW() WHERE id=%s AND lifecycle_status IN ('draft','validation_failed')", (row["instance_id"],))
                db.commit()
            finally:
                cur.close()

    def ensure_declared_capabilities(self, instance_id: int, *, adapter_version: str, capability_versions: Mapping[str, str]) -> None:
        with self._connection() as db:
            cur = db.cursor()
            try:
                for capability_key, capability_version in capability_versions.items():
                    cur.execute(
                        """INSERT INTO qd_provider_instance_capabilities
                           (instance_id,capability_key,eligibility_status,adapter_version,capability_version,verification_evidence)
                           VALUES (%s,%s,'unverified',%s,%s,'{"code":"not_yet_verified","status":"declared"}'::jsonb)
                           ON CONFLICT (instance_id,capability_key) DO NOTHING""",
                        (instance_id, capability_key, adapter_version, capability_version),
                    )
                db.commit()
            finally:
                cur.close()

    def find_account_identity_conflict(self, adapter_key: str, identity: str, *, exclude_instance_id: int):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id FROM qd_provider_instances WHERE adapter_key=%s AND provider_account_identity=%s
                       AND lifecycle_status<>'retired' AND id<>%s LIMIT 1""",
                    (adapter_key, identity, exclude_instance_id),
                )
                row = cur.fetchone()
                return int(row["id"]) if row else None
            finally:
                cur.close()

    def find_other_unidentified_active(self, adapter_key: str, *, exclude_instance_id: int):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id FROM qd_provider_instances WHERE adapter_key=%s AND provider_account_identity IS NULL
                       AND lifecycle_status IN ('active','draining') AND id<>%s LIMIT 1""",
                    (adapter_key, exclude_instance_id),
                )
                row = cur.fetchone()
                return int(row["id"]) if row else None
            finally:
                cur.close()

    def activate_validated_pending(self, **values):
        instance = values["instance"]
        pending = values["pending"]
        results = values["results"]
        identity = values["provider_account_identity"]
        with self._connection() as db:
            cur = db.cursor()
            sanitized_error = None
            try:
                cur.execute("SELECT provider_account_identity FROM qd_provider_instances WHERE id=%s FOR UPDATE", (instance.instance_id,))
                locked = cur.fetchone()
                if not locked:
                    raise ProviderCredentialError("Provider Instance is unavailable")
                current_identity = locked["provider_account_identity"]
                cur.execute("SELECT id FROM qd_provider_credentials WHERE instance_id=%s AND status='active' FOR UPDATE", (instance.instance_id,))
                old_active = cur.fetchone()
                if old_active and current_identity != identity:
                    raise ProviderAccountConflictError("Provider account identity changed during credential activation")
                if identity is not None:
                    cur.execute("""SELECT id FROM qd_provider_instances WHERE adapter_key=%s AND provider_account_identity=%s
                        AND lifecycle_status<>'retired' AND id<>%s FOR UPDATE""", (instance.adapter_key, identity, instance.instance_id))
                    if cur.fetchone():
                        raise ProviderAccountConflictError("Provider account already belongs to another Provider Instance")
                else:
                    cur.execute("""SELECT id FROM qd_provider_instances WHERE adapter_key=%s AND provider_account_identity IS NULL
                        AND lifecycle_status IN ('active','draining') AND id<>%s FOR UPDATE""", (instance.adapter_key, instance.instance_id))
                    if cur.fetchone():
                        raise ProviderAccountConflictError("Adapter permits only one unidentified active Provider Instance")
                cur.execute("SELECT id FROM qd_provider_credentials WHERE id=%s AND instance_id=%s AND status='pending' FOR UPDATE", (pending.credential_id, instance.instance_id))
                if not cur.fetchone():
                    raise ProviderCredentialError("pending Provider credential changed concurrently")
                cur.execute("UPDATE qd_provider_credentials SET status='destroyed',ciphertext='',destroyed_at=NOW() WHERE instance_id=%s AND status='active'", (instance.instance_id,))
                cur.execute(
                    """UPDATE qd_provider_credentials SET status='active',activated_at=NOW(),validation_summary=%s::jsonb
                       WHERE id=%s""",
                    (_evidence(results), pending.credential_id),
                )
                for result in results:
                    status = "eligible" if result.outcome == "eligible" else ("ineligible" if result.outcome == "ineligible" else "unverified")
                    cur.execute(
                        """INSERT INTO qd_provider_instance_capabilities
                           (instance_id,capability_key,eligibility_status,adapter_version,capability_version,
                            verification_evidence,last_verified_at,next_verification_at)
                           VALUES (%s,%s,%s,%s,%s,%s::jsonb,CASE WHEN %s='unverified' THEN NULL ELSE NOW() END,NOW()+INTERVAL '7 days')
                           ON CONFLICT (instance_id,capability_key) DO UPDATE SET
                            eligibility_status=CASE
                              WHEN qd_provider_instance_capabilities.eligibility_status='disabled' THEN 'disabled'
                              ELSE EXCLUDED.eligibility_status
                            END,
                            adapter_version=EXCLUDED.adapter_version,capability_version=EXCLUDED.capability_version,
                            verification_evidence=EXCLUDED.verification_evidence,last_verified_at=EXCLUDED.last_verified_at,
                            next_verification_at=EXCLUDED.next_verification_at,
                            disabled_reason=CASE
                              WHEN qd_provider_instance_capabilities.eligibility_status='disabled'
                                THEN qd_provider_instance_capabilities.disabled_reason
                              ELSE ''
                            END,updated_at=NOW()""",
                        (instance.instance_id, result.capability_key, status, pending.credential_schema_version,
                         str(result.evidence.get("capability_version") or ""), json.dumps(dict(result.evidence)), status),
                    )
                cur.execute(
                    """UPDATE qd_provider_instances SET provider_account_identity=%s,account_identity_verified_at=NOW(),
                       lifecycle_status=CASE WHEN lifecycle_status IN ('draft','validation_failed')
                         THEN 'active' ELSE lifecycle_status END,
                       activated_at=CASE WHEN lifecycle_status IN ('draft','validation_failed')
                         THEN COALESCE(activated_at,NOW()) ELSE activated_at END,
                       updated_by=%s,updated_at=NOW()
                       WHERE id=%s""",
                    (identity, values["actor_user_id"], instance.instance_id),
                )
                self._audit(cur, actor_user_id=values["actor_user_id"], action="provider_credential_activated",
                            target_id=instance.instance_id, reason=values["reason"],
                            before={"active_credential": bool(old_active), "account_identity_present": current_identity is not None},
                            after={"credential_version": pending.credential_version, "account_identity_present": identity is not None,
                                   "eligible_capabilities": sorted(r.capability_key for r in results if r.eligible)})
                db.commit()
            except ProviderCredentialError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                sanitized_error = ProviderCredentialError("Provider credential activation failed")
            finally:
                cur.close()
            if sanitized_error is not None:
                raise sanitized_error
        return self.get_credential_status(instance.instance_id)

    def get_credential_status(self, instance_id: int):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT
                       MAX(credential_version) FILTER (WHERE status='active') AS active_version,
                       MAX(credential_version) FILTER (WHERE status='pending') AS pending_version,
                       MAX(encryption_key_id) FILTER (WHERE status='active') AS active_key_id,
                       MAX(encryption_key_id) FILTER (WHERE status='pending') AS pending_key_id,
                       MAX(activated_at) FILTER (WHERE status='active') AS active_updated_at,
                       MAX(created_at) FILTER (WHERE status='pending') AS pending_updated_at
                       FROM qd_provider_credentials WHERE instance_id=%s""",
                    (instance_id,),
                )
                row = dict(cur.fetchone())
                return CredentialStatus(configured=row["active_version"] is not None, **row)
            finally:
                cur.close()

    def mark_capabilities_for_revalidation(self, instance_id: int, capability_keys, *, trigger: str) -> None:
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_instance_capabilities SET
                       eligibility_status=CASE WHEN %s IN ('configuration_change','credential_change','permission_evidence')
                         AND eligibility_status<>'disabled' THEN 'unverified' ELSE eligibility_status END,
                       verification_evidence=jsonb_build_object('trigger',%s,'status','pending'),
                       next_verification_at=NOW(),updated_at=NOW()
                       WHERE instance_id=%s AND capability_key=ANY(%s)""",
                    (trigger, trigger, instance_id, list(capability_keys)),
                )
                db.commit()
            finally:
                cur.close()

    def apply_capability_revalidation(self, instance_id: int, results, *, trigger: str) -> None:
        with self._connection() as db:
            cur = db.cursor()
            try:
                for result in results:
                    if result.outcome == "eligible":
                        status_sql = "'eligible'"
                    elif result.outcome == "ineligible":
                        status_sql = "'ineligible'"
                    else:
                        status_sql = "eligibility_status"
                    cur.execute(
                        f"""UPDATE qd_provider_instance_capabilities SET eligibility_status={status_sql},
                            verification_evidence=%s::jsonb,
                            last_verified_at=CASE WHEN %s='transient_failure' THEN last_verified_at ELSE NOW() END,
                            next_verification_at=NOW()+INTERVAL '7 days',updated_at=NOW()
                            WHERE instance_id=%s AND capability_key=%s AND eligibility_status<>'disabled'""",
                        (json.dumps({"trigger": trigger, **dict(result.evidence)}), result.outcome, instance_id, result.capability_key),
                    )
                db.commit()
            finally:
                cur.close()

    def disable_capability(self, instance_id: int, capability_key: str, *, actor_user_id: int | None, reason: str) -> None:
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_provider_instance_capabilities SET eligibility_status='disabled',disabled_reason=%s,updated_at=NOW()
                       WHERE instance_id=%s AND capability_key=%s AND eligibility_status<>'disabled'""",
                    (reason, instance_id, capability_key),
                )
                if cur.rowcount != 1:
                    raise ProviderCredentialError("Capability is not verified for this Provider Instance")
                self._audit(cur, actor_user_id=actor_user_id, action="provider_instance_capability_disabled",
                            target_id=instance_id, reason=reason, before={"capability_key": capability_key},
                            after={"capability_key": capability_key, "eligibility_status": "disabled"})
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def list_due_revalidation_instances(self, *, limit: int):
        with self._connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT DISTINCT instance_id FROM qd_provider_instance_capabilities
                       WHERE eligibility_status IN ('eligible','ineligible','unverified')
                         AND next_verification_at<=NOW() ORDER BY instance_id LIMIT %s""",
                    (limit,),
                )
                return [int(row["instance_id"]) for row in cur.fetchall() or []]
            finally:
                cur.close()
