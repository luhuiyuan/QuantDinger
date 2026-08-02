"""Fail-open persistence for Routed Data Requests and Provider Attempts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Mapping

from app.services.external_data_request_logs import sanitize_summary

from .router import ProviderAttemptRecord, RoutedDataResult


class PostgresRouterObservabilitySink:
    def __init__(self, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            from app.utils.db import get_db_connection

            connection_factory = get_db_connection
        self.connection_factory = connection_factory
        self._lock = Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._failure_count = 0

    def _degraded(self, operation: str) -> None:
        with self._lock:
            self._failure_count += 1
        try:
            from app.observability.metrics import DATA_ROUTING_OBSERVABILITY_FAILURES

            DATA_ROUTING_OBSERVABILITY_FAILURES.labels(operation=operation).inc()
        except Exception:
            pass

    def health(self) -> Mapping[str, Any]:
        return {
            "status": "degraded" if self._failure_count else "ok",
            "pending_request_contexts": len(self._requests),
            "failure_count": self._failure_count,
            "failure_metric": "quantdinger_data_routing_observability_failures_total",
        }

    def request_started(self, **values: Any) -> None:
        request_id = values["routed_request_id"]
        with self._lock:
            self._requests[request_id] = dict(values)
        try:
            with self.connection_factory() as db:
                cur = db.cursor()
                try:
                    cur.execute(
                        """INSERT INTO qd_routed_data_requests
                           (routed_request_id,capability_key,policy_revision_id,calling_feature,request_mode,
                            subject_summary,constraints,deadline_at,final_outcome)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'failed')""",
                        (
                            request_id, values["capability_key"], values["policy_revision_id"],
                            sanitize_summary(values["calling_feature"], max_length=160), values["mode"],
                            sanitize_summary(values.get("subject"), max_length=512),
                            json.dumps(dict(values.get("constraints") or {}), sort_keys=True, default=str),
                            values["deadline"],
                        ),
                    )
                    db.commit()
                finally:
                    cur.close()
        except Exception:
            self._degraded("request_started")

    def attempt_recorded(self, attempt: ProviderAttemptRecord) -> None:
        with self._lock:
            request = dict(self._requests.get(attempt.routed_request_id) or {})
        result = self._attempt_result(attempt)
        try:
            with self.connection_factory() as db:
                cur = db.cursor()
                try:
                    cur.execute(
                        """INSERT INTO qd_external_data_request_logs
                           (provider,data_domain,operation,call_source,subject_summary,fallback_index,
                            retry_count,result,error_summary,request_id,routed_request_id,provider_instance_id,
                            policy_revision_id,attempt_order,skip_reason,retry_summary,quality_outcome)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
                        (
                            sanitize_summary(attempt.adapter_key, max_length=64),
                            sanitize_summary(request.get("capability_key"), max_length=48),
                            "routed_fetch", sanitize_summary(request.get("calling_feature"), max_length=96),
                            sanitize_summary(request.get("subject"), max_length=512),
                            max(0, attempt.attempt_order - 1), attempt.retry_summary.retries, result,
                            sanitize_summary(attempt.error_category or attempt.skip_reason, max_length=1024),
                            attempt.routed_request_id, attempt.routed_request_id, attempt.instance_id,
                            request.get("policy_revision_id"), attempt.attempt_order,
                            sanitize_summary(attempt.skip_reason, max_length=120),
                            json.dumps({
                                "transport_calls": attempt.retry_summary.transport_calls,
                                "retries": attempt.retry_summary.retries,
                                "categories": list(attempt.retry_summary.categories),
                                "exhausted": attempt.retry_summary.exhausted,
                                "quota_reservation_ids": list(attempt.quota_reservation_ids),
                            }, sort_keys=True),
                            json.dumps({
                                "outcome": attempt.outcome,
                                "failures": list(attempt.quality_failures),
                                "warnings": [item.code for item in attempt.warnings],
                            }, sort_keys=True),
                        ),
                    )
                    db.commit()
                finally:
                    cur.close()
        except Exception:
            self._degraded("attempt_recorded")

    def request_completed(self, **values: Any) -> None:
        request_id = values["routed_request_id"]
        result: RoutedDataResult | None = values.get("result")
        explanation = list(result.provenance.explanation if result else values.get("explanation") or ())
        try:
            with self.connection_factory() as db:
                cur = db.cursor()
                try:
                    cur.execute(
                        """UPDATE qd_routed_data_requests SET final_outcome=%s,selected_instance_id=%s,
                           selected_cache_key_hash=%s,provider_public_name=%s,acquired_at=%s,freshness=%s,
                           quality_warnings=%s::jsonb,explanation=%s::jsonb,completed_at=NOW()
                           WHERE routed_request_id=%s""",
                        (
                            values["outcome"], result.provenance.provider_instance_id if result else None,
                            result.provenance.cache_key.rsplit(":", 1)[-1][:64] if result else "",
                            sanitize_summary(result.provider_public_name, max_length=160) if result else "",
                            result.acquired_at if result else None,
                            "stale" if result and result.freshness == "stale" else ("fresh" if result else "none"),
                            json.dumps([{"code": item.code, "message": item.message} for item in (result.quality_warnings if result else ())], sort_keys=True),
                            json.dumps({"steps": explanation[:200], "attempt_count": values.get("attempt_count", 0)}, sort_keys=True),
                            request_id,
                        ),
                    )
                    db.commit()
                finally:
                    cur.close()
        except Exception:
            self._degraded("request_completed")
        finally:
            with self._lock:
                self._requests.pop(request_id, None)

    @staticmethod
    def _attempt_result(attempt: ProviderAttemptRecord) -> str:
        if attempt.outcome == "accepted":
            return "success"
        if attempt.outcome == "rejected":
            return "invalid_response"
        if attempt.outcome == "skipped":
            return "disabled" if attempt.skip_reason in {"instance_disabled", "capability_disabled"} else "skipped"
        category = attempt.error_category.lower()
        if "timeout" in category or category == "deadline":
            return "timeout"
        if "rate" in category or "quota" in category:
            return "rate_limited"
        if any(item in category for item in ("network", "connection", "dns")):
            return "network_error"
        return "provider_error"

    def cleanup_expired(
        self,
        *,
        success_days: int = 30,
        abnormal_days: int = 90,
        summary_days: int = 90,
        batch_size: int = 1000,
    ) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        bounded = max(1, min(int(batch_size), 10000))
        with self.connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """WITH candidates AS (
                         SELECT id FROM qd_external_data_request_logs
                         WHERE (result='success' AND occurred_at<%s)
                            OR (result<>'success' AND occurred_at<%s)
                         ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT %s
                       ) DELETE FROM qd_external_data_request_logs log USING candidates
                       WHERE log.id=candidates.id RETURNING log.id""",
                    (now - timedelta(days=max(1, success_days)), now - timedelta(days=max(1, abnormal_days)), bounded),
                )
                attempts = len(cur.fetchall() or [])
                cur.execute(
                    """WITH candidates AS (
                         SELECT routed_request_id FROM qd_routed_data_requests WHERE created_at<%s
                         ORDER BY created_at,routed_request_id FOR UPDATE SKIP LOCKED LIMIT %s
                       ) DELETE FROM qd_routed_data_requests request USING candidates
                       WHERE request.routed_request_id=candidates.routed_request_id RETURNING request.routed_request_id""",
                    (now - timedelta(days=max(1, summary_days)), bounded),
                )
                summaries = len(cur.fetchall() or [])
                db.commit()
                return {"attempts": attempts, "summaries": summaries}
            finally:
                cur.close()
