"""Safe, durable observability for logical external data provider attempts."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.observability.context import request_id_context
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)
_cache_stats_lock = threading.Lock()
_cache_stats_pending: dict[tuple[str, str], list[int]] = {}
_cache_stats_last_flush = 0.0
_CACHE_STATS_FLUSH_SECONDS = 30.0
_CACHE_STATS_FLUSH_THRESHOLD = 100


class ExternalDataRequestResult(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    NETWORK_ERROR = "network_error"
    INVALID_RESPONSE = "invalid_response"
    DISABLED = "disabled"
    SKIPPED = "skipped"


SUCCESSFUL_RESULTS = frozenset({
    ExternalDataRequestResult.SUCCESS,
    ExternalDataRequestResult.DISABLED,
    ExternalDataRequestResult.SKIPPED,
})
RESULT_VALUES = frozenset(item.value for item in ExternalDataRequestResult)
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|authorization|cookie|token|secret|password|signature|credential)")
_QUERY_RE = re.compile(r"([?&](?:api[_-]?key|authorization|cookie|token|secret|password|signature|credential)=[^&#\s]+)", re.I)
_MAX_SUBJECT = 512
_MAX_ERROR = 1024


def sanitize_summary(value: Any, *, max_length: int = _MAX_SUBJECT) -> str:
    """Return a compact safe summary; never keep URLs' query strings or secrets."""
    if value is None:
        return ""
    if isinstance(value, dict):
        safe = {
            str(key): "[REDACTED]" if _SECRET_KEY_RE.search(str(key)) else sanitize_summary(item, max_length=128)
            for key, item in value.items()
        }
        text = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    elif isinstance(value, (list, tuple, set)):
        text = json.dumps([sanitize_summary(item, max_length=128) for item in value], ensure_ascii=False)
    else:
        text = str(value)
        try:
            parsed = urlsplit(text)
            if parsed.scheme and parsed.netloc:
                text = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            pass
    text = _QUERY_RE.sub("[REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|cookie|token|secret|password|signature|credential)"
        r"\s*[:=]\s*(?:bearer\s+)?[^,;\s]+",
        r"\1=[REDACTED]",
        text,
    )
    text = " ".join(text.split())
    return text[:max_length]


@dataclass(frozen=True)
class ExternalDataRequestLog:
    provider: str
    data_domain: str
    operation: str
    result: ExternalDataRequestResult | str
    call_source: str = ""
    subject_summary: str = ""
    fallback_index: int = 0
    retry_count: int = 0
    duration_ms: int = 0
    http_status: int | None = None
    error_summary: str = ""
    request_id: str = ""

    def normalized(self) -> dict[str, Any]:
        result = self.result.value if isinstance(self.result, ExternalDataRequestResult) else str(self.result)
        if result not in RESULT_VALUES:
            raise ValueError(f"Unsupported external data request result: {result}")
        return {
            "provider": sanitize_summary(self.provider, max_length=64),
            "data_domain": sanitize_summary(self.data_domain, max_length=48),
            "operation": sanitize_summary(self.operation, max_length=96),
            "result": result,
            "call_source": sanitize_summary(self.call_source, max_length=96),
            "subject_summary": sanitize_summary(self.subject_summary),
            "fallback_index": max(0, int(self.fallback_index)),
            "retry_count": max(0, int(self.retry_count)),
            "duration_ms": max(0, int(self.duration_ms)),
            "http_status": int(self.http_status) if self.http_status is not None else None,
            "error_summary": sanitize_summary(self.error_summary, max_length=_MAX_ERROR),
            "request_id": sanitize_summary(self.request_id or request_id_context.get(), max_length=128),
        }


class ExternalDataRequestLogService:
    """Repository operations. ``record`` is deliberately fail-open."""

    def enabled(self) -> bool:
        from app.services.external_data_request_settings import load_external_data_request_log_settings
        return load_external_data_request_log_settings().enabled

    def record(self, entry: ExternalDataRequestLog) -> bool:
        if not self.enabled():
            return False
        try:
            payload = entry.normalized()
            if not payload["provider"] or not payload["data_domain"] or not payload["operation"]:
                raise ValueError("provider, data_domain and operation are required")
            with get_db_connection() as db:
                cur = db.cursor()
                try:
                    cur.execute(
                        """INSERT INTO qd_external_data_request_logs
                           (provider, data_domain, operation, call_source, subject_summary,
                            fallback_index, retry_count, duration_ms, result, http_status,
                            error_summary, request_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (payload["provider"], payload["data_domain"], payload["operation"], payload["call_source"],
                         payload["subject_summary"], payload["fallback_index"], payload["retry_count"],
                         payload["duration_ms"], payload["result"], payload["http_status"],
                         payload["error_summary"], payload["request_id"]),
                    )
                    db.commit()
                finally:
                    cur.close()
            return True
        except Exception as exc:  # observability must never alter provider behaviour
            logger.warning("external_data_request_log_write_failed: %s", sanitize_summary(exc, max_length=300))
            return False

    def record_cache(self, *, data_domain: str, operation: str, hit: bool) -> None:
        self.record_cache_batch({(data_domain, operation): (int(hit), int(not hit))})

    def record_cache_batch(self, values: dict[tuple[str, str], tuple[int, int]]) -> None:
        """Persist a compact cache-stat batch; errors remain fail-open."""
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                try:
                    for (data_domain, operation), (hits, misses) in values.items():
                        if not hits and not misses:
                            continue
                        cur.execute(
                        """INSERT INTO qd_external_data_cache_stats
                           (bucket_date, data_domain, operation, cache_hits, cache_misses)
                           VALUES (CURRENT_DATE, %s, %s, %s, %s)
                           ON CONFLICT (bucket_date, data_domain, operation) DO UPDATE
                           SET cache_hits = qd_external_data_cache_stats.cache_hits + EXCLUDED.cache_hits,
                               cache_misses = qd_external_data_cache_stats.cache_misses + EXCLUDED.cache_misses,
                               updated_at = NOW()""",
                            (sanitize_summary(data_domain, max_length=48), sanitize_summary(operation, max_length=96), int(hits), int(misses)),
                        )
                    db.commit()
                finally:
                    cur.close()
        except Exception as exc:
            logger.warning("external_data_cache_stat_write_failed: %s", sanitize_summary(exc, max_length=300))

    def latest_cleanup_run(self) -> dict[str, Any] | None:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_external_data_log_cleanup_runs ORDER BY started_at DESC, id DESC LIMIT 1")
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def list_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        provider: str = "",
        data_domain: str = "",
        result: str = "",
        request_id: str = "",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        clauses, params = ["TRUE"], []
        for column, value, max_length in (
            ("provider", provider, 64), ("data_domain", data_domain, 48),
            ("result", result, 32), ("request_id", request_id, 128),
        ):
            value = sanitize_summary(value, max_length=max_length)
            if value:
                if column == "result" and value not in RESULT_VALUES:
                    raise ValueError("Unsupported result filter")
                clauses.append(f"{column} = %s")
                params.append(value)
        if started_at:
            clauses.append("occurred_at >= %s")
            params.append(started_at)
        if ended_at:
            clauses.append("occurred_at <= %s")
            params.append(ended_at)
        where = " AND ".join(clauses)
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(f"SELECT COUNT(*) AS total FROM qd_external_data_request_logs WHERE {where}", params)
                total = int(cur.fetchone()["total"])
                cur.execute(
                    f"""SELECT id, occurred_at, provider, data_domain, operation, call_source,
                               subject_summary, fallback_index, retry_count, duration_ms, result,
                               http_status, error_summary, request_id
                        FROM qd_external_data_request_logs WHERE {where}
                        ORDER BY occurred_at DESC, id DESC LIMIT %s OFFSET %s""",
                    [*params, page_size, (page - 1) * page_size],
                )
                return {"items": [self._public_row(dict(row)) for row in cur.fetchall()], "total": total, "page": page, "page_size": page_size}
            finally:
                cur.close()

    def get_log(self, log_id: int) -> dict[str, Any] | None:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_external_data_request_logs WHERE id = %s", (int(log_id),))
                row = cur.fetchone()
                return self._public_row(dict(row)) if row else None
            finally:
                cur.close()

    def overview(self, *, hours: int = 24) -> dict[str, Any]:
        hours = min(24 * 31, max(1, int(hours)))
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE result = 'success') AS success_count,
                              COALESCE(jsonb_object_agg(result, count), '{}'::jsonb) AS result_counts
                       FROM (SELECT result, COUNT(*) AS count
                             FROM qd_external_data_request_logs
                             WHERE occurred_at >= NOW() - (%s * INTERVAL '1 hour') GROUP BY result) grouped""",
                    (hours,),
                )
                row = dict(cur.fetchone())
                cur.execute(
                    """SELECT COALESCE(SUM(cache_hits), 0) AS hits, COALESCE(SUM(cache_misses), 0) AS misses
                       FROM qd_external_data_cache_stats
                       WHERE bucket_date >= CURRENT_DATE - ((%s + 23) / 24)::integer""",
                    (hours,),
                )
                cache = dict(cur.fetchone())
            finally:
                cur.close()
        total, hits, misses = int(row.get("total") or 0), int(cache["hits"]), int(cache["misses"])
        return {
            "hours": hours, "external_calls": total, "success_count": int(row.get("success_count") or 0),
            "success_rate": round((int(row.get("success_count") or 0) / total) * 100, 2) if total else None,
            "result_counts": row.get("result_counts") or {}, "cache_hits": hits, "cache_misses": misses,
            "cache_hit_rate": round((hits / (hits + misses)) * 100, 2) if hits + misses else None,
        }

    def start_cleanup_run(self) -> int:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("INSERT INTO qd_external_data_log_cleanup_runs (status) VALUES ('running') RETURNING id")
                run_id = int(cur.fetchone()["id"])
                db.commit()
                return run_id
            finally:
                cur.close()

    def finish_cleanup_run(self, run_id: int, *, deleted_count: int, error: Any = "") -> None:
        error_summary = sanitize_summary(error, max_length=_MAX_ERROR)
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_external_data_log_cleanup_runs
                       SET status = %s, completed_at = NOW(), deleted_count = %s, error_summary = %s
                       WHERE id = %s""",
                    ("failed" if error_summary else "succeeded", max(0, int(deleted_count)), error_summary, int(run_id)),
                )
                db.commit()
            finally:
                cur.close()

    @staticmethod
    def _public_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: sanitize_summary(value, max_length=_MAX_ERROR if key == "error_summary" else _MAX_SUBJECT)
            if key in {"subject_summary", "error_summary", "call_source", "request_id", "provider", "data_domain", "operation"}
            else value
            for key, value in row.items()
        }

    def cleanup_expired(self, *, successful_retention_days: int, error_retention_days: int, batch_size: int) -> int:
        """Delete a bounded batch; safe for repeated concurrent invocations."""
        successful_cutoff = datetime.now(timezone.utc) - timedelta(days=successful_retention_days)
        error_cutoff = datetime.now(timezone.utc) - timedelta(days=error_retention_days)
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """WITH candidates AS (
                         SELECT id FROM qd_external_data_request_logs
                         WHERE (result IN ('success', 'disabled', 'skipped') AND occurred_at < %s)
                            OR (result NOT IN ('success', 'disabled', 'skipped') AND occurred_at < %s)
                         ORDER BY occurred_at, id FOR UPDATE SKIP LOCKED LIMIT %s
                       ) DELETE FROM qd_external_data_request_logs log
                       USING candidates WHERE log.id = candidates.id RETURNING log.id""",
                    (successful_cutoff, error_cutoff, batch_size),
                )
                deleted = len(cur.fetchall())
                db.commit()
                return deleted
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()


class ProviderAttempt:
    """Explicit logical provider-attempt boundary for data adapters.

    Adapters may call :meth:`set_http_status`, :meth:`skip`, or :meth:`fail`
    before leaving the context. Unclassified exceptions are kept as provider
    errors rather than leaking provider exception internals into storage.
    """

    def __init__(
        self,
        *,
        provider: str,
        data_domain: str,
        operation: str,
        call_source: str = "",
        subject_summary: Any = "",
        fallback_index: int = 0,
        retry_count: int = 0,
        service: ExternalDataRequestLogService | None = None,
    ) -> None:
        self.provider = provider
        self.data_domain = data_domain
        self.operation = operation
        self.call_source = call_source
        self.subject_summary = subject_summary
        self.fallback_index = fallback_index
        self.retry_count = retry_count
        self.service = service or ExternalDataRequestLogService()
        self.http_status: int | None = None
        self.result: ExternalDataRequestResult = ExternalDataRequestResult.SUCCESS
        self.error_summary = ""
        self._started = 0.0

    def __enter__(self) -> "ProviderAttempt":
        self._started = time.perf_counter()
        return self

    def set_http_status(self, status: int | None) -> "ProviderAttempt":
        self.http_status = int(status) if status is not None else None
        if self.http_status == 429:
            self.result = ExternalDataRequestResult.RATE_LIMITED
        elif self.http_status is not None and self.http_status >= 500:
            self.result = ExternalDataRequestResult.PROVIDER_ERROR
        return self

    def skip(self, *, disabled: bool = False, reason: Any = "") -> "ProviderAttempt":
        self.result = ExternalDataRequestResult.DISABLED if disabled else ExternalDataRequestResult.SKIPPED
        self.error_summary = sanitize_summary(reason, max_length=_MAX_ERROR)
        return self

    def fail(self, result: ExternalDataRequestResult | str, error: Any = "") -> "ProviderAttempt":
        self.result = ExternalDataRequestResult(result)
        self.error_summary = sanitize_summary(error, max_length=_MAX_ERROR)
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc is not None:
            self.result = _classify_exception(exc)
            self.error_summary = sanitize_summary(exc, max_length=_MAX_ERROR)
        duration_ms = round(max(0.0, time.perf_counter() - self._started) * 1000)
        self.service.record(ExternalDataRequestLog(
            provider=self.provider,
            data_domain=self.data_domain,
            operation=self.operation,
            result=self.result,
            call_source=self.call_source,
            subject_summary=self.subject_summary,
            fallback_index=self.fallback_index,
            retry_count=self.retry_count,
            duration_ms=duration_ms,
            http_status=self.http_status,
            error_summary=self.error_summary,
        ))
        return False


def _classify_exception(exc: BaseException) -> ExternalDataRequestResult:
    """Classify common transport errors without taking a hard dependency on requests."""
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timeout" in message:
        return ExternalDataRequestResult.TIMEOUT
    if "connection" in name or "network" in name or "dns" in message:
        return ExternalDataRequestResult.NETWORK_ERROR
    if "rate" in name or "429" in message or "too many requests" in message:
        return ExternalDataRequestResult.RATE_LIMITED
    if isinstance(exc, (ValueError, TypeError, KeyError, json.JSONDecodeError)):
        return ExternalDataRequestResult.INVALID_RESPONSE
    return ExternalDataRequestResult.PROVIDER_ERROR


def record_cache_observation(*, data_domain: str, operation: str, hit: bool) -> None:
    """Accumulate cache metrics in-process and flush at most every 30 seconds.

    These metrics are intentionally lossy on process shutdown: provider calls
    must never gain a synchronous database dependency merely for cache stats.
    """
    global _cache_stats_last_flush
    now = time.monotonic()
    batch: dict[tuple[str, str], tuple[int, int]] | None = None
    with _cache_stats_lock:
        key = (sanitize_summary(data_domain, max_length=48), sanitize_summary(operation, max_length=96))
        counts = _cache_stats_pending.setdefault(key, [0, 0])
        counts[0 if hit else 1] += 1
        total = sum(sum(item) for item in _cache_stats_pending.values())
        if total >= _CACHE_STATS_FLUSH_THRESHOLD or now - _cache_stats_last_flush >= _CACHE_STATS_FLUSH_SECONDS:
            batch = {key: (counts[0], counts[1]) for key, counts in _cache_stats_pending.items()}
            _cache_stats_pending.clear()
            _cache_stats_last_flush = now
    if batch:
        ExternalDataRequestLogService().record_cache_batch(batch)
