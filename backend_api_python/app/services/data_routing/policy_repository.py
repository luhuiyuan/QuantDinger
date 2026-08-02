"""PostgreSQL persistence for versioned data-routing policies."""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Mapping, Sequence

from .policy import (
    RoutingPolicyConflictError,
    RoutingPolicyEntry,
    RoutingPolicyError,
    RoutingPolicyRevision,
    RoutingPolicyState,
)


class PostgresRoutingPolicyRepository:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory

    @staticmethod
    def _audit(
        cur,
        *,
        actor_user_id: int | None,
        action: str,
        target_id: int,
        reason: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> None:
        cur.execute(
            """INSERT INTO qd_data_source_audit
               (actor_user_id,action,target_type,target_id,reason,before_summary,after_summary,correlation_id,outcome)
               VALUES (%s,%s,'routing_policy',%s,%s,%s::jsonb,%s::jsonb,%s,'succeeded')""",
            (
                actor_user_id,
                action,
                str(target_id),
                reason,
                json.dumps(dict(before), sort_keys=True),
                json.dumps(dict(after), sort_keys=True),
                uuid.uuid4().hex,
            ),
        )

    @staticmethod
    def _entry(row) -> RoutingPolicyEntry:
        return RoutingPolicyEntry(
            instance_id=int(row["instance_id"]),
            position=int(row["position"]),
            eligibility_requirements=dict(row.get("eligibility_requirements") or {}),
            stricter_quality_profile=dict(row.get("stricter_quality_profile") or {}),
        )

    def _revision(self, cur, revision_id: int | None, capability_key: str) -> RoutingPolicyRevision | None:
        if revision_id is None:
            return None
        cur.execute(
            """SELECT id AS revision_id,policy_id,revision_number,status,based_on_revision_id,
                      quality_profile,impact_preview,change_reason
               FROM qd_data_routing_revisions WHERE id=%s""",
            (revision_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RoutingPolicyError("routing policy revision does not exist")
        cur.execute(
            """SELECT instance_id,position,eligibility_requirements,stricter_quality_profile
               FROM qd_data_routing_entries WHERE revision_id=%s ORDER BY position""",
            (revision_id,),
        )
        return RoutingPolicyRevision(
            revision_id=int(row["revision_id"]),
            policy_id=int(row["policy_id"]),
            capability_key=capability_key,
            revision_number=int(row["revision_number"]),
            status=str(row["status"]),
            based_on_revision_id=int(row["based_on_revision_id"]) if row["based_on_revision_id"] is not None else None,
            quality_profile=dict(row["quality_profile"] or {}),
            impact_preview=dict(row["impact_preview"] or {}),
            change_reason=str(row["change_reason"] or ""),
            entries=tuple(self._entry(entry) for entry in (cur.fetchall() or [])),
        )

    def _state_from_row(self, cur, row) -> RoutingPolicyState:
        capability_key = str(row["capability_key"])
        return RoutingPolicyState(
            policy_id=int(row["id"]),
            capability_key=capability_key,
            policy_version=int(row["policy_version"]),
            enabled=bool(row["enabled"]),
            disabled_reason=str(row["disabled_reason"] or ""),
            effective_revision=self._revision(cur, row["effective_revision_id"], capability_key),
            draft_revision=self._revision(cur, row["draft_revision_id"], capability_key),
        )

    def get_policy(self, capability_key: str):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT id,capability_key,policy_version,enabled,disabled_reason,
                              effective_revision_id,draft_revision_id
                       FROM qd_data_routing_policies WHERE capability_key=%s""",
                    (capability_key,),
                )
                row = cur.fetchone()
                return self._state_from_row(cur, row) if row else None
            finally:
                cur.close()

    def list_revisions(self, capability_key: str):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT r.id FROM qd_data_routing_revisions r
                       JOIN qd_data_routing_policies p ON p.id=r.policy_id
                       WHERE p.capability_key=%s ORDER BY r.revision_number DESC,r.id DESC""",
                    (capability_key,),
                )
                revision_ids = [int(row["id"]) for row in (cur.fetchall() or [])]
                return tuple(self._revision(cur, revision_id, capability_key) for revision_id in revision_ids)
            finally:
                cur.close()

    @staticmethod
    def _ensure_and_lock_policy(cur, capability_key: str):
        cur.execute(
            """INSERT INTO qd_data_routing_policies (capability_key)
               VALUES (%s) ON CONFLICT (capability_key) DO NOTHING""",
            (capability_key,),
        )
        cur.execute(
            """SELECT id,capability_key,policy_version,enabled,disabled_reason,
                      effective_revision_id,draft_revision_id
               FROM qd_data_routing_policies WHERE capability_key=%s FOR UPDATE""",
            (capability_key,),
        )
        row = cur.fetchone()
        if not row:
            raise RoutingPolicyError("Data Capability is not materialized")
        return row

    @staticmethod
    def _check_expected_version(row, expected_policy_version: int | None) -> None:
        current = int(row["policy_version"])
        pristine = row["effective_revision_id"] is None and row["draft_revision_id"] is None and current == 1
        if expected_policy_version is None:
            if not pristine:
                raise RoutingPolicyConflictError("routing policy version is required for concurrent modification")
        elif current != int(expected_policy_version):
            raise RoutingPolicyConflictError(
                "routing policy changed concurrently",
                details={"expected_policy_version": int(expected_policy_version), "current_policy_version": current},
            )

    @staticmethod
    def _validate_entries(cur, capability_key: str, entries: Sequence[RoutingPolicyEntry]) -> None:
        if not entries:
            return
        instance_ids = [entry.instance_id for entry in entries]
        cur.execute(
            """SELECT id,lifecycle_status FROM qd_provider_instances
               WHERE id=ANY(%s) FOR SHARE""",
            (instance_ids,),
        )
        instances = {int(row["id"]): row for row in (cur.fetchall() or [])}
        missing = [instance_id for instance_id in instance_ids if instance_id not in instances]
        if missing:
            raise RoutingPolicyError("routing policy references missing Provider Instance", details={"instance_ids": missing})
        inactive = [
            instance_id for instance_id in instance_ids if str(instances[instance_id]["lifecycle_status"]) != "active"
        ]
        if inactive:
            raise RoutingPolicyError("routing policy requires active Provider Instances", details={"instance_ids": inactive})
        cur.execute(
            """SELECT instance_id,eligibility_status FROM qd_provider_instance_capabilities
               WHERE capability_key=%s AND instance_id=ANY(%s) FOR SHARE""",
            (capability_key, instance_ids),
        )
        capabilities = {int(row["instance_id"]): row for row in (cur.fetchall() or [])}
        ineligible = [
            instance_id
            for instance_id in instance_ids
            if str(capabilities.get(instance_id, {}).get("eligibility_status") or "") != "eligible"
        ]
        if ineligible:
            raise RoutingPolicyError(
                "routing policy requires verified eligible Instance Capabilities",
                details={"instance_ids": ineligible, "capability_key": capability_key},
            )

    @staticmethod
    def _insert_entries(cur, revision_id: int, entries: Sequence[RoutingPolicyEntry]) -> None:
        for entry in entries:
            cur.execute(
                """INSERT INTO qd_data_routing_entries
                   (revision_id,position,instance_id,eligibility_requirements,stricter_quality_profile)
                   VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)""",
                (
                    revision_id,
                    entry.position,
                    entry.instance_id,
                    json.dumps(dict(entry.eligibility_requirements), sort_keys=True),
                    json.dumps(dict(entry.stricter_quality_profile), sort_keys=True),
                ),
            )

    def save_draft(self, **values):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                policy = self._ensure_and_lock_policy(cur, values["capability_key"])
                self._check_expected_version(policy, values["expected_policy_version"])
                entries = tuple(values["entries"])
                self._validate_entries(cur, values["capability_key"], entries)
                draft_id = policy["draft_revision_id"]
                if draft_id is None:
                    cur.execute(
                        "SELECT COALESCE(MAX(revision_number),0)+1 AS revision_number FROM qd_data_routing_revisions WHERE policy_id=%s",
                        (policy["id"],),
                    )
                    revision_number = int(cur.fetchone()["revision_number"])
                    cur.execute(
                        """INSERT INTO qd_data_routing_revisions
                           (policy_id,revision_number,status,based_on_revision_id,quality_profile,created_by)
                           VALUES (%s,%s,'draft',%s,%s::jsonb,%s) RETURNING id""",
                        (
                            policy["id"],
                            revision_number,
                            policy["effective_revision_id"],
                            json.dumps(dict(values["quality_profile"]), sort_keys=True),
                            values["actor_user_id"],
                        ),
                    )
                    draft_id = int(cur.fetchone()["id"])
                else:
                    cur.execute(
                        """UPDATE qd_data_routing_revisions SET quality_profile=%s::jsonb
                           WHERE id=%s AND policy_id=%s AND status='draft'""",
                        (json.dumps(dict(values["quality_profile"]), sort_keys=True), draft_id, policy["id"]),
                    )
                    if cur.rowcount != 1:
                        raise RoutingPolicyConflictError("routing policy draft changed concurrently")
                    cur.execute("DELETE FROM qd_data_routing_entries WHERE revision_id=%s", (draft_id,))
                self._insert_entries(cur, draft_id, entries)
                cur.execute(
                    """UPDATE qd_data_routing_policies SET draft_revision_id=%s,
                       policy_version=policy_version+1,updated_at=NOW() WHERE id=%s""",
                    (draft_id, policy["id"]),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
        return self.get_policy(values["capability_key"])

    def publish_draft(self, **values):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                policy = self._ensure_and_lock_policy(cur, values["capability_key"])
                self._check_expected_version(policy, values["expected_policy_version"])
                draft_id = policy["draft_revision_id"]
                if draft_id is None:
                    raise RoutingPolicyError("routing policy has no draft revision")
                cur.execute(
                    """SELECT instance_id,position,eligibility_requirements,stricter_quality_profile
                       FROM qd_data_routing_entries WHERE revision_id=%s ORDER BY position FOR UPDATE""",
                    (draft_id,),
                )
                entries = tuple(self._entry(row) for row in (cur.fetchall() or []))
                if not entries:
                    raise RoutingPolicyError("enabled routing policy cannot publish an empty Instance list")
                self._validate_entries(cur, values["capability_key"], entries)
                before = {
                    "effective_revision_id": policy["effective_revision_id"],
                    "enabled": bool(policy["enabled"]),
                }
                if policy["effective_revision_id"] is not None:
                    cur.execute(
                        """UPDATE qd_data_routing_revisions SET status='superseded'
                           WHERE id=%s AND policy_id=%s AND status='published'""",
                        (policy["effective_revision_id"], policy["id"]),
                    )
                    if cur.rowcount != 1:
                        raise RoutingPolicyConflictError("effective routing revision changed concurrently")
                cur.execute(
                    """UPDATE qd_data_routing_revisions SET status='published',impact_preview=%s::jsonb,
                       change_reason=%s,published_by=%s,published_at=NOW()
                       WHERE id=%s AND policy_id=%s AND status='draft'""",
                    (
                        json.dumps(dict(values["impact_preview"]), sort_keys=True),
                        values["reason"],
                        values["actor_user_id"],
                        draft_id,
                        policy["id"],
                    ),
                )
                if cur.rowcount != 1:
                    raise RoutingPolicyConflictError("routing policy draft changed concurrently")
                cur.execute(
                    """UPDATE qd_data_routing_policies SET effective_revision_id=%s,draft_revision_id=NULL,
                       enabled=TRUE,disabled_reason='',policy_version=policy_version+1,updated_at=NOW()
                       WHERE id=%s""",
                    (draft_id, policy["id"]),
                )
                self._audit(
                    cur,
                    actor_user_id=values["actor_user_id"],
                    action="routing_policy_published",
                    target_id=policy["id"],
                    reason=values["reason"],
                    before=before,
                    after={"effective_revision_id": draft_id, "enabled": True},
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
        return self.get_policy(values["capability_key"])

    def restore_revision(self, **values):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                policy = self._ensure_and_lock_policy(cur, values["capability_key"])
                self._check_expected_version(policy, values["expected_policy_version"])
                if policy["draft_revision_id"] is not None:
                    raise RoutingPolicyConflictError("discard the current draft before restoring a historical revision")
                cur.execute(
                    """SELECT id,quality_profile FROM qd_data_routing_revisions
                       WHERE id=%s AND policy_id=%s AND status IN ('published','superseded')""",
                    (values["source_revision_id"], policy["id"]),
                )
                source = cur.fetchone()
                if not source:
                    raise RoutingPolicyError("historical routing revision does not exist")
                cur.execute(
                    """SELECT instance_id,position,eligibility_requirements,stricter_quality_profile
                       FROM qd_data_routing_entries WHERE revision_id=%s ORDER BY position""",
                    (source["id"],),
                )
                entries = tuple(self._entry(row) for row in (cur.fetchall() or []))
                self._validate_entries(cur, values["capability_key"], entries)
                cur.execute(
                    "SELECT COALESCE(MAX(revision_number),0)+1 AS revision_number FROM qd_data_routing_revisions WHERE policy_id=%s",
                    (policy["id"],),
                )
                revision_number = int(cur.fetchone()["revision_number"])
                cur.execute(
                    """INSERT INTO qd_data_routing_revisions
                       (policy_id,revision_number,status,based_on_revision_id,quality_profile,created_by)
                       VALUES (%s,%s,'draft',%s,%s::jsonb,%s) RETURNING id""",
                    (
                        policy["id"],
                        revision_number,
                        source["id"],
                        json.dumps(dict(source["quality_profile"] or {}), sort_keys=True),
                        values["actor_user_id"],
                    ),
                )
                draft_id = int(cur.fetchone()["id"])
                self._insert_entries(cur, draft_id, entries)
                cur.execute(
                    """UPDATE qd_data_routing_policies SET draft_revision_id=%s,
                       policy_version=policy_version+1,updated_at=NOW() WHERE id=%s""",
                    (draft_id, policy["id"]),
                )
                self._audit(
                    cur,
                    actor_user_id=values["actor_user_id"],
                    action="routing_policy_revision_restored_to_draft",
                    target_id=policy["id"],
                    reason=str(values.get("reason") or "restore_historical_revision"),
                    before={"source_revision_id": source["id"]},
                    after={"draft_revision_id": draft_id},
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
        return self.get_policy(values["capability_key"])

    def disable_capability(self, **values):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                policy = self._ensure_and_lock_policy(cur, values["capability_key"])
                self._check_expected_version(policy, values["expected_policy_version"])
                cur.execute(
                    """UPDATE qd_data_routing_policies SET enabled=FALSE,disabled_reason=%s,
                       policy_version=policy_version+1,updated_at=NOW() WHERE id=%s""",
                    (values["reason"], policy["id"]),
                )
                self._audit(
                    cur,
                    actor_user_id=values["actor_user_id"],
                    action="routing_capability_disabled",
                    target_id=policy["id"],
                    reason=values["reason"],
                    before={"enabled": bool(policy["enabled"])},
                    after={"enabled": False},
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
        return self.get_policy(values["capability_key"])

    def discard_draft(self, **values):
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                policy = self._ensure_and_lock_policy(cur, values["capability_key"])
                self._check_expected_version(policy, values["expected_policy_version"])
                draft_id = policy["draft_revision_id"]
                if draft_id is None:
                    raise RoutingPolicyError("routing policy has no draft revision")
                cur.execute(
                    """UPDATE qd_data_routing_revisions SET status='discarded',change_reason=%s
                       WHERE id=%s AND policy_id=%s AND status='draft'""",
                    (values["reason"], draft_id, policy["id"]),
                )
                if cur.rowcount != 1:
                    raise RoutingPolicyConflictError("routing policy draft changed concurrently")
                cur.execute(
                    """UPDATE qd_data_routing_policies SET draft_revision_id=NULL,
                       policy_version=policy_version+1,updated_at=NOW() WHERE id=%s""",
                    (policy["id"],),
                )
                self._audit(
                    cur,
                    actor_user_id=values["actor_user_id"],
                    action="routing_policy_draft_discarded",
                    target_id=policy["id"],
                    reason=values["reason"],
                    before={"draft_revision_id": draft_id},
                    after={"draft_revision_id": None},
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()
        return self.get_policy(values["capability_key"])
