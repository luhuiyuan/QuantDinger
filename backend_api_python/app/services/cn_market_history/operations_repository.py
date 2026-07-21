"""Persistence for China A-share sync operations, quality, and coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from app.utils.db import get_db_connection

from .instruments import parse_cn_instrument
from .models import (
    AdjustmentMode,
    CoverageReport,
    ProviderProbe,
    QualityFinding,
    SyncStatus,
    SyncTarget,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


class CNMarketHistoryOperationsRepository:
    def __init__(self, connection_factory: Callable = get_db_connection) -> None:
        self._connection_factory = connection_factory

    def create_sync_run(
        self,
        targets: Sequence[SyncTarget],
        *,
        requested_by: int | None,
        request_kind: str = "targeted",
        parent_run_id: str | None = None,
        request_payload: Mapping | None = None,
    ) -> str:
        if not targets:
            raise ValueError("At least one sync target is required")
        run_id = uuid4().hex
        start_date = min(target.start_date for target in targets)
        end_date = max(target.end_date for target in targets)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_cn_history_sync_runs
                    (run_id, parent_run_id, requested_by, request_kind, target_start,
                     target_end, total_symbols, request_payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        run_id,
                        parent_run_id,
                        requested_by,
                        request_kind,
                        start_date,
                        end_date,
                        len(targets),
                        _json(request_payload or {}),
                    ),
                )
                for target in targets:
                    cur.execute(
                        """
                        INSERT INTO qd_cn_history_sync_targets
                        (run_id, instrument, target_start, target_end)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            run_id,
                            target.instrument.canonical,
                            target.start_date,
                            target.end_date,
                        ),
                    )
                db.commit()
            finally:
                cur.close()
        return run_id

    def get_sync_run(self, run_id: str) -> dict | None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_cn_history_sync_runs WHERE run_id = %s", (run_id,))
                run = cur.fetchone()
                if not run:
                    return None
                cur.execute(
                    "SELECT * FROM qd_cn_history_sync_targets WHERE run_id = %s ORDER BY id",
                    (run_id,),
                )
                result = dict(run)
                result["targets"] = cur.fetchall()
                return result
            finally:
                cur.close()

    def list_sync_runs(self, *, limit: int = 50) -> list[dict]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    "SELECT * FROM qd_cn_history_sync_runs ORDER BY created_at DESC LIMIT %s",
                    (safe_limit,),
                )
                return cur.fetchall()
            finally:
                cur.close()

    def find_overlapping_active_run(
        self,
        instruments: Sequence[str],
        start_date,
        end_date,
        *,
        exclude_run_id: str | None = None,
    ) -> dict | None:
        canonical = [parse_cn_instrument(item).canonical for item in instruments]
        if not canonical:
            return None
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT DISTINCT r.*
                    FROM qd_cn_history_sync_runs r
                    JOIN qd_cn_history_sync_targets t ON t.run_id = r.run_id
                    WHERE r.status IN ('pending', 'running', 'paused')
                      AND (%s IS NULL OR r.run_id <> %s)
                      AND t.instrument = ANY(%s)
                      AND t.target_start <= %s
                      AND t.target_end >= %s
                    ORDER BY r.created_at DESC
                    LIMIT 1
                    """,
                    (exclude_run_id, exclude_run_id, canonical, end_date, start_date),
                )
                return cur.fetchone()
            finally:
                cur.close()

    def list_sync_targets(self, run_id: str, statuses: Sequence[SyncStatus]) -> list[dict]:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT * FROM qd_cn_history_sync_targets
                    WHERE run_id = %s AND status = ANY(%s)
                    ORDER BY id ASC
                    """,
                    (run_id, [status.value for status in statuses]),
                )
                return cur.fetchall()
            finally:
                cur.close()

    def update_run(self, run_id: str, status: SyncStatus, **fields: object) -> None:
        allowed = {
            "succeeded_symbols",
            "failed_symbols",
            "skipped_symbols",
            "current_instrument",
            "last_error_code",
            "last_error",
            "result_summary",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported run fields: {sorted(unknown)}")
        assignments = ["status = %s", "updated_at = NOW()"]
        params: list[object] = [status.value]
        for key, value in fields.items():
            assignments.append(f"{key} = %s" + ("::jsonb" if key == "result_summary" else ""))
            params.append(_json(value) if key == "result_summary" else value)
        if status is SyncStatus.RUNNING:
            assignments.append("started_at = COALESCE(started_at, NOW())")
        if status in {
            SyncStatus.SUCCEEDED,
            SyncStatus.PARTIAL,
            SyncStatus.FAILED,
            SyncStatus.CANCELLED,
        }:
            assignments.append("finished_at = NOW()")
        params.append(run_id)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    f"UPDATE qd_cn_history_sync_runs SET {', '.join(assignments)} WHERE run_id = %s",
                    tuple(params),
                )
                db.commit()
            finally:
                cur.close()

    def update_target(self, run_id: str, instrument: str, status: SyncStatus, **fields: object) -> None:
        canonical = parse_cn_instrument(instrument).canonical
        allowed = {
            "page_offset",
            "checkpoint_date",
            "attempts",
            "bars_written",
            "actions_written",
            "last_error_code",
            "last_error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported target fields: {sorted(unknown)}")
        assignments = ["status = %s", "updated_at = NOW()"]
        params: list[object] = [status.value]
        for key, value in fields.items():
            assignments.append(f"{key} = %s")
            params.append(value)
        if status is SyncStatus.RUNNING:
            assignments.append("started_at = COALESCE(started_at, NOW())")
        if status in {
            SyncStatus.SUCCEEDED,
            SyncStatus.FAILED,
            SyncStatus.CANCELLED,
            SyncStatus.SKIPPED,
        }:
            assignments.append("finished_at = NOW()")
        params.extend((run_id, canonical))
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    f"""
                    UPDATE qd_cn_history_sync_targets
                    SET {', '.join(assignments)}
                    WHERE run_id = %s AND instrument = %s
                    """,
                    tuple(params),
                )
                db.commit()
            finally:
                cur.close()

    def upsert_provider_probe(self, provider: str, probe: ProviderProbe, *, selected: bool) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                if selected:
                    cur.execute(
                        "UPDATE qd_cn_provider_health SET selected = FALSE WHERE provider = %s",
                        (provider,),
                    )
                cur.execute(
                    """
                    INSERT INTO qd_cn_provider_health
                    (provider, host, healthy, selected, latency_ms, consecutive_failures,
                     cooldown_until, last_checked_at, last_success_at, last_error_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider, host) DO UPDATE SET
                        healthy = EXCLUDED.healthy,
                        selected = EXCLUDED.selected,
                        latency_ms = EXCLUDED.latency_ms,
                        consecutive_failures = EXCLUDED.consecutive_failures,
                        cooldown_until = EXCLUDED.cooldown_until,
                        last_checked_at = EXCLUDED.last_checked_at,
                        last_success_at = EXCLUDED.last_success_at,
                        last_error_code = EXCLUDED.last_error_code,
                        updated_at = NOW()
                    """,
                    (
                        provider,
                        probe.host,
                        probe.healthy,
                        selected,
                        probe.latency_ms,
                        probe.consecutive_failures,
                        probe.cooldown_until,
                        probe.checked_at,
                        probe.last_success_at,
                        probe.error_code,
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def list_provider_health(self, provider: str = "easy_tdx") -> list[dict]:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT * FROM qd_cn_provider_health
                    WHERE provider = %s ORDER BY selected DESC, healthy DESC, latency_ms ASC NULLS LAST
                    """,
                    (provider,),
                )
                return cur.fetchall()
            finally:
                cur.close()

    def upsert_quality_findings(self, findings: Sequence[QualityFinding]) -> int:
        now = datetime.now(timezone.utc)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                for finding in findings:
                    identity = "|".join(
                        (
                            finding.instrument,
                            finding.finding_type,
                            finding.start_date.isoformat(),
                            finding.end_date.isoformat(),
                        )
                    )
                    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    cur.execute(
                        """
                        INSERT INTO qd_cn_history_quality_findings
                        (fingerprint, instrument, finding_type, severity, start_date,
                         end_date, evidence, first_detected_at, last_detected_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (fingerprint) DO UPDATE SET
                            severity = EXCLUDED.severity,
                            status = 'open',
                            evidence = EXCLUDED.evidence,
                            last_detected_at = EXCLUDED.last_detected_at,
                            resolved_at = NULL
                        """,
                        (
                            fingerprint,
                            finding.instrument,
                            finding.finding_type,
                            finding.severity.value,
                            finding.start_date,
                            finding.end_date,
                            _json(finding.evidence),
                            now,
                            now,
                        ),
                    )
                db.commit()
            finally:
                cur.close()
        return len(findings)

    def resolve_absent_findings(self, instrument: str, active_fingerprints: Sequence[str]) -> int:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                if active_fingerprints:
                    cur.execute(
                        """
                        UPDATE qd_cn_history_quality_findings
                        SET status = 'resolved', resolved_at = NOW()
                        WHERE instrument = %s AND status = 'open'
                          AND NOT (fingerprint = ANY(%s))
                        """,
                        (canonical, list(active_fingerprints)),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE qd_cn_history_quality_findings
                        SET status = 'resolved', resolved_at = NOW()
                        WHERE instrument = %s AND status = 'open'
                        """,
                        (canonical,),
                    )
                count = cur.rowcount
                db.commit()
                return count
            finally:
                cur.close()

    def list_quality_findings(self, instrument: str, *, status: str = "open") -> list[dict]:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT * FROM qd_cn_history_quality_findings
                    WHERE instrument = %s AND status = %s
                    ORDER BY start_date ASC, severity DESC
                    """,
                    (canonical, status),
                )
                return cur.fetchall()
            finally:
                cur.close()

    def upsert_coverage(
        self,
        report: CoverageReport,
        *,
        provider: str,
        mode: AdjustmentMode,
        factor_version: str | None = None,
        last_successful_sync_at: datetime | None = None,
    ) -> None:
        gaps = [
            {
                "start_date": gap.start_date.isoformat(),
                "end_date": gap.end_date.isoformat(),
                "reason": gap.reason,
                "dates": [item.isoformat() for item in gap.dates],
            }
            for gap in report.gaps
        ]
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_cn_history_coverage
                    (instrument, provider, adjustment_mode, first_trade_date, last_trade_date,
                     expected_sessions, actual_sessions, missing_sessions, blocking_findings,
                     complete, data_version, factor_version, gaps, last_successful_sync_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (instrument, provider, adjustment_mode) DO UPDATE SET
                        first_trade_date = EXCLUDED.first_trade_date,
                        last_trade_date = EXCLUDED.last_trade_date,
                        expected_sessions = EXCLUDED.expected_sessions,
                        actual_sessions = EXCLUDED.actual_sessions,
                        missing_sessions = EXCLUDED.missing_sessions,
                        blocking_findings = EXCLUDED.blocking_findings,
                        complete = EXCLUDED.complete,
                        data_version = EXCLUDED.data_version,
                        factor_version = EXCLUDED.factor_version,
                        gaps = EXCLUDED.gaps,
                        last_successful_sync_at = COALESCE(
                            EXCLUDED.last_successful_sync_at,
                            qd_cn_history_coverage.last_successful_sync_at
                        ),
                        updated_at = NOW()
                    """,
                    (
                        report.instrument,
                        provider,
                        mode.value,
                        report.first_trade_date,
                        report.last_trade_date,
                        report.expected_sessions,
                        report.actual_sessions,
                        sum(len(gap.dates) or 1 for gap in report.gaps),
                        report.blocking_findings,
                        report.complete,
                        report.data_version,
                        factor_version,
                        _json(gaps),
                        last_successful_sync_at,
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def get_coverage(
        self,
        instrument: str,
        *,
        provider: str = "easy_tdx",
        mode: AdjustmentMode = AdjustmentMode.RAW,
    ) -> dict | None:
        canonical = parse_cn_instrument(instrument).canonical
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT * FROM qd_cn_history_coverage
                    WHERE instrument = %s AND provider = %s AND adjustment_mode = %s
                    """,
                    (canonical, provider, mode.value),
                )
                return cur.fetchone()
            finally:
                cur.close()

    def get_coverage_summary(self) -> dict:
        """Return a read-only aggregate for the administrator overview."""
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT instrument) AS total_instruments,
                        COUNT(*) FILTER (
                            WHERE adjustment_mode = 'raw' AND complete
                        ) AS complete_raw,
                        COUNT(*) FILTER (
                            WHERE adjustment_mode = 'raw' AND NOT complete
                        ) AS incomplete_raw,
                        COALESCE(SUM(missing_sessions) FILTER (
                            WHERE adjustment_mode = 'raw'
                        ), 0) AS missing_sessions,
                        COALESCE(SUM(blocking_findings) FILTER (
                            WHERE adjustment_mode = 'raw'
                        ), 0) AS blocking_findings,
                        MAX(last_successful_sync_at) AS last_successful_sync_at,
                        MAX(updated_at) AS updated_at
                    FROM qd_cn_history_coverage
                    """
                )
                return cur.fetchone() or {}
            finally:
                cur.close()

    def write_audit(
        self,
        *,
        actor_user_id: int | None,
        action: str,
        run_id: str | None,
        request_scope: Mapping,
        result_status: str,
    ) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_cn_history_operation_audit
                    (actor_user_id, action, run_id, request_scope, result_status)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    """,
                    (actor_user_id, action, run_id, _json(request_scope), result_status),
                )
                db.commit()
            finally:
                cur.close()
