"""PostgreSQL persistence for append-only annual fundamental observations."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from app.utils.db import get_db_connection
from app.services.cn_market_history.instruments import parse_cn_instrument


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash(payload: Mapping) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


class CNFundamentalHistoryRepository:
    def __init__(self, connection_factory: Callable = get_db_connection):
        self._connection_factory = connection_factory

    def ensure_instrument(self, instrument: str) -> None:
        """Materialize a current catalog symbol before writing FK-bound history."""
        parsed = parse_cn_instrument(instrument)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    "SELECT 1 AS present FROM qd_cn_instruments WHERE instrument=%s",
                    (parsed.canonical,),
                )
                if cur.fetchone():
                    return

                cur.execute(
                    """SELECT name FROM qd_market_symbols
                       WHERE market='CNStock'
                         AND UPPER(symbol) IN (%s,%s,%s)
                       ORDER BY is_active DESC,id ASC LIMIT 1""",
                    (
                        parsed.code,
                        f"{parsed.code}.{parsed.exchange}",
                        parsed.canonical.upper(),
                    ),
                )
                catalog_row = cur.fetchone()
                if not catalog_row:
                    raise ValueError("cn_fundamental.instrument_not_found")

                name = str(catalog_row.get("name") or "")
                source = "market_symbols"
                content_hash = _hash(
                    {
                        "instrument": parsed.canonical,
                        "name": name,
                        "security_type": "ordinary_share",
                        "source": source,
                    }
                )
                cur.execute(
                    """INSERT INTO qd_cn_instruments
                       (instrument,code,exchange,name,security_type,source,source_version,content_hash)
                       VALUES (%s,%s,%s,%s,'ordinary_share',%s,'',%s)
                       ON CONFLICT (instrument) DO NOTHING""",
                    (
                        parsed.canonical,
                        parsed.code,
                        parsed.exchange,
                        name,
                        source,
                        content_hash,
                    ),
                )
                db.commit()
            finally:
                cur.close()

    @contextmanager
    def advisory_lock(self, key: str = "cn-fundamental-history") -> Iterator[bool]:
        with self._connection_factory() as db:
            cur = db.cursor()
            acquired = False
            try:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (key,))
                acquired = bool((cur.fetchone() or {}).get("acquired"))
                yield acquired
            finally:
                if acquired:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                cur.close()

    def append_observation(
        self,
        *,
        instrument: str,
        period_end: date,
        available_at: date,
        source: str,
        raw_payload: Mapping,
        fields: Mapping[str, Any],
        source_version: str = "",
        request_context: Mapping | None = None,
        announcement_ref: Mapping | None = None,
        mapping_version: str = "v1",
    ) -> int:
        content_hash = _hash(raw_payload)
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"fundamental:{instrument}:{period_end}:{source}",))
                cur.execute(
                    """SELECT id, observation_version FROM qd_cn_fundamental_observations
                       WHERE instrument=%s AND period_end=%s AND source=%s
                       ORDER BY observation_version DESC, id DESC LIMIT 1""",
                    (instrument, period_end, source),
                )
                previous = cur.fetchone()
                cur.execute(
                    """INSERT INTO qd_cn_fundamental_observations
                       (instrument, period_end, available_at, source, source_version,
                        content_hash, raw_payload, request_context, announcement_ref,
                        supersedes_observation_id, observation_version)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
                       ON CONFLICT (instrument,period_end,available_at,source,content_hash) DO NOTHING
                       RETURNING id""",
                    (
                        instrument, period_end, available_at, source, source_version,
                        content_hash, _json(raw_payload), _json(request_context), _json(announcement_ref),
                        previous["id"] if previous else None,
                        int(previous["observation_version"]) + 1 if previous else 1,
                    ),
                )
                inserted = cur.fetchone()
                if inserted:
                    observation_id = inserted["id"]
                    for code, item in fields.items():
                        evidence = item if isinstance(item, Mapping) and "value" in item else None
                        value = item.get("value") if evidence else item
                        numeric = value if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None
                        text = None if numeric is not None or value is None else str(value)
                        cur.execute(
                            """INSERT INTO qd_cn_fundamental_field_values
                               (observation_id,field_code,value_numeric,value_text,mapping_version,source_field,evidence)
                               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                            (
                                observation_id, code, numeric, text, mapping_version,
                                str(evidence.get("sourceField") or code) if evidence else code,
                                _json(evidence or {}),
                            ),
                        )
                else:
                    cur.execute(
                        """SELECT id FROM qd_cn_fundamental_observations
                           WHERE instrument=%s AND period_end=%s AND available_at=%s
                             AND source=%s AND content_hash=%s""",
                        (instrument, period_end, available_at, source, content_hash),
                    )
                    observation_id = cur.fetchone()["id"]
                db.commit()
                return int(observation_id)
            finally:
                cur.close()

    def list_versions(self, instrument: str, period_end: date) -> list[dict]:
        return self._fetchall(
            """SELECT id, instrument, period_end, available_at, source, source_version,
                      observation_version, content_hash, announcement_ref, collected_at
               FROM qd_cn_fundamental_observations
               WHERE instrument=%s AND period_end=%s
               ORDER BY source, observation_version, id""",
            (instrument, period_end),
        )

    def load_as_of(self, instrument: str, as_of: date) -> list[dict]:
        # Rank the latest PIT-safe version per source, then merge by field so a
        # partial official observation cannot erase primary-source fields.
        rows = self._fetchall(
            """WITH ranked AS (
                   SELECT DISTINCT ON (period_end,source) id
                   FROM qd_cn_fundamental_observations
                   WHERE instrument=%s AND available_at<=%s
                   ORDER BY period_end,source,available_at DESC, observation_version DESC, id DESC
               )
               SELECT o.period_end,o.available_at,o.source,o.source_version,
                      o.observation_version,o.announcement_ref,
                      f.field_code,f.value_numeric,f.value_text
               FROM ranked r
               JOIN qd_cn_fundamental_observations o ON o.id=r.id
               JOIN qd_cn_fundamental_field_values f ON f.observation_id=o.id
               ORDER BY o.period_end,CASE WHEN o.source='cninfo' THEN 1 ELSE 0 END""",
            (instrument, as_of),
        )
        grouped: dict[date, dict] = {}
        for row in rows:
            period = row["period_end"]
            result = grouped.setdefault(period, {
                "period_end": period,
                "available_at": row["available_at"],
                "sources": [],
            })
            result["available_at"] = max(result["available_at"], row["available_at"])
            source_meta = {"source": row["source"], "source_version": row["source_version"], "observation_version": row["observation_version"], "announcement_ref": row["announcement_ref"]}
            if source_meta not in result["sources"]:
                result["sources"].append(source_meta)
            result[row["field_code"]] = row["value_numeric"] if row["value_numeric"] is not None else row["value_text"]
        return list(grouped.values())

    def upsert_metrics(self, instrument: str, period_end: date, available_at: date, metrics: Mapping[str, Any], *, formula_version: str) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                for code, metric in metrics.items():
                    cur.execute(
                        """INSERT INTO qd_cn_fundamental_metric_values
                           (instrument,as_of_period_end,available_at,metric_code,value_numeric,status,formula_version,evidence)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT (instrument,as_of_period_end,available_at,metric_code,formula_version)
                           DO UPDATE SET value_numeric=EXCLUDED.value_numeric,status=EXCLUDED.status,
                                         evidence=EXCLUDED.evidence,calculated_at=NOW()""",
                        (instrument, period_end, available_at, code, metric.value, metric.status, formula_version, _json(metric.evidence)),
                    )
                db.commit()
            finally:
                cur.close()

    def upsert_industry_classification(
        self, *, instrument: str, taxonomy: str, industry_code: str,
        industry_name: str, effective_start: date, effective_end: date | None,
        source: str, source_version: str = "", evidence: Mapping | None = None,
    ) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_cn_industry_classifications
                       (instrument,taxonomy,industry_code,industry_name,effective_start,effective_end,source,source_version,evidence)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (instrument,taxonomy,industry_code,effective_start,source)
                       DO UPDATE SET industry_name=EXCLUDED.industry_name,effective_end=EXCLUDED.effective_end,
                                     source_version=EXCLUDED.source_version,evidence=EXCLUDED.evidence""",
                    (instrument, taxonomy, industry_code, industry_name, effective_start, effective_end, source, source_version, _json(evidence)),
                )
                db.commit()
            finally:
                cur.close()

    def load_industry_as_of(self, instrument: str, as_of: date) -> dict | None:
        rows = self._fetchall(
            """SELECT * FROM qd_cn_industry_classifications
               WHERE instrument=%s AND taxonomy='csrc' AND effective_start<=%s
                 AND (effective_end IS NULL OR effective_end>=%s)
               ORDER BY effective_start DESC,id DESC LIMIT 1""",
            (instrument, as_of, as_of),
        )
        return rows[0] if rows else None

    def refresh_coverage(self, instrument: str, *, required_fields: Sequence[str] = ()) -> dict:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT MIN(period_end) first_period_end, MAX(period_end) last_period_end,
                              COUNT(DISTINCT id) observation_count,
                              COUNT(DISTINCT period_end) complete_year_count,
                              MAX(available_at) last_available_at
                       FROM qd_cn_fundamental_observations WHERE instrument=%s""",
                    (instrument,),
                )
                summary = dict(cur.fetchone() or {})
                missing = []
                if required_fields:
                    cur.execute(
                        """WITH latest AS (
                               SELECT id FROM qd_cn_fundamental_observations
                               WHERE instrument=%s
                               ORDER BY period_end DESC,available_at DESC,observation_version DESC,id DESC
                               LIMIT 1
                           )
                           SELECT DISTINCT f.field_code FROM qd_cn_fundamental_field_values f
                           JOIN latest l ON l.id=f.observation_id
                           WHERE f.field_code=ANY(%s)""",
                        (instrument, list(required_fields)),
                    )
                    present = {row["field_code"] for row in cur.fetchall()}
                    missing = sorted(set(required_fields) - present)
                cur.execute(
                    """INSERT INTO qd_cn_fundamental_coverage
                       (instrument,first_period_end,last_period_end,observation_count,complete_year_count,missing_fields,last_available_at)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                       ON CONFLICT (instrument) DO UPDATE SET
                         first_period_end=EXCLUDED.first_period_end,last_period_end=EXCLUDED.last_period_end,
                         observation_count=EXCLUDED.observation_count,complete_year_count=EXCLUDED.complete_year_count,
                         missing_fields=EXCLUDED.missing_fields,last_available_at=EXCLUDED.last_available_at,updated_at=NOW()""",
                    (instrument, summary.get("first_period_end"), summary.get("last_period_end"), summary.get("observation_count", 0), summary.get("complete_year_count", 0), _json(missing), summary.get("last_available_at")),
                )
                db.commit()
                return {"instrument": instrument, **summary, "missing_fields": missing}
            finally:
                cur.close()

    def coverage_summary(self, *, limit: int = 200) -> list[dict]:
        return self._fetchall("SELECT * FROM qd_cn_fundamental_coverage ORDER BY updated_at DESC LIMIT %s", (max(1, min(limit, 1000)),))

    def list_eligible_instruments(self) -> list[str]:
        rows = self._fetchall(
            """SELECT instrument AS symbol FROM qd_cn_instruments
               WHERE exchange IN ('SH','SZ') AND security_type='ordinary_share'
               UNION
               SELECT symbol FROM qd_market_symbols
               WHERE market='CNStock' AND is_active=1
               ORDER BY symbol""",
            (),
        )
        instruments = []
        for row in rows:
            try:
                instruments.append(parse_cn_instrument(row["symbol"]).canonical)
            except Exception:
                continue
        return list(dict.fromkeys(instruments))

    def persist_screening(self, instrument: str, period_end: date, available_at: date, screening, *, formula_version: str) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                evidence = {"outcomes": [item.__dict__ for item in screening.outcomes]}
                cur.execute(
                    """INSERT INTO qd_cn_fundamental_quality_results
                       (instrument,period_end,available_at,status,formula_version,evidence)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (instrument,period_end,available_at,formula_version)
                       DO UPDATE SET status=EXCLUDED.status,evidence=EXCLUDED.evidence,calculated_at=NOW()""",
                    (instrument, period_end, available_at, screening.status, formula_version, _json(evidence)),
                )
                for target in screening.verification_targets:
                    cur.execute(
                        """INSERT INTO qd_cn_fundamental_verification_targets
                           (instrument,period_end,trigger_metric,trigger_value,threshold_value,evidence)
                           VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                           ON CONFLICT (instrument,period_end,trigger_metric)
                           DO UPDATE SET trigger_value=EXCLUDED.trigger_value,threshold_value=EXCLUDED.threshold_value,
                                         evidence=EXCLUDED.evidence,
                                         status=CASE WHEN qd_cn_fundamental_verification_targets.status='verified'
                                                     THEN 'verified' ELSE 'pending' END""",
                        (instrument, period_end, target.metric_code, target.value, target.threshold, _json(target.evidence)),
                    )
                db.commit()
            finally:
                cur.close()

    def record_reconciliation(self, target_id: int, field_code: str, result, *, instrument: str, period_end: date) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_cn_fundamental_reconciliations
                       (target_id,field_code,primary_value,official_value,difference_pct,status,evidence)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (target_id,field_code) DO UPDATE SET
                         primary_value=EXCLUDED.primary_value,official_value=EXCLUDED.official_value,
                         difference_pct=EXCLUDED.difference_pct,status=EXCLUDED.status,evidence=EXCLUDED.evidence,
                         created_at=NOW()""",
                    (target_id, field_code, result.primary_value, result.official_value, result.difference_pct, result.status, _json(result.evidence)),
                )
                if result.status in {"warning", "blocking"}:
                    cur.execute(
                        """INSERT INTO qd_cn_fundamental_quality_issues
                           (instrument,period_end,issue_code,severity,source,evidence)
                           VALUES (%s,%s,%s,%s,'cninfo_reconciliation',%s::jsonb)
                           ON CONFLICT (instrument,period_end,issue_code,source) DO UPDATE SET
                             severity=EXCLUDED.severity,status='open',evidence=EXCLUDED.evidence,
                             resolved_at=NULL,created_at=NOW()""",
                        (instrument, period_end, f"source_difference:{field_code}", result.status, _json({"differencePct": result.difference_pct, **result.evidence})),
                    )
                db.commit()
            finally:
                cur.close()

    def finalize_verification_target(self, target_id: int) -> str:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT status FROM qd_cn_fundamental_reconciliations WHERE target_id=%s", (target_id,))
                statuses = {row["status"] for row in cur.fetchall()}
                status = "blocked" if "blocking" in statuses else "unavailable" if not statuses or "unavailable" in statuses else "warning" if "warning" in statuses else "verified"
                cur.execute("UPDATE qd_cn_fundamental_verification_targets SET status=%s,resolved_at=CASE WHEN %s='unavailable' THEN NULL ELSE NOW() END WHERE id=%s", (status, status, target_id))
                db.commit()
                return status
            finally:
                cur.close()

    def list_quality_issues(self, *, status: str = "open", limit: int = 200) -> list[dict]:
        return self._fetchall("SELECT * FROM qd_cn_fundamental_quality_issues WHERE status=%s ORDER BY created_at DESC LIMIT %s", (status, max(1, min(limit, 1000))))

    def list_verification_targets(self, *, status: str = "pending", limit: int = 200) -> list[dict]:
        return self._fetchall("SELECT * FROM qd_cn_fundamental_verification_targets WHERE status=%s ORDER BY created_at LIMIT %s", (status, max(1, min(limit, 1000))))

    def create_run(self, instruments: Sequence[str], *, requested_by: int | None, request_kind: str = "backfill", request_payload: Mapping | None = None) -> str:
        if not instruments:
            raise ValueError("cn_fundamental.instruments_required")
        run_id = uuid4().hex
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_cn_fundamental_sync_runs
                       (run_id,requested_by,request_kind,total_symbols,request_payload)
                       VALUES (%s,%s,%s,%s,%s::jsonb)""",
                    (run_id, requested_by, request_kind, len(instruments), _json(request_payload)),
                )
                for instrument in instruments:
                    cur.execute("INSERT INTO qd_cn_fundamental_sync_targets (run_id,instrument) VALUES (%s,%s)", (run_id, instrument))
                db.commit()
            finally:
                cur.close()
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        rows = self._fetchall("SELECT * FROM qd_cn_fundamental_sync_runs WHERE run_id=%s", (run_id,))
        if not rows:
            return None
        run = dict(rows[0])
        run["targets"] = self._fetchall("SELECT * FROM qd_cn_fundamental_sync_targets WHERE run_id=%s ORDER BY id", (run_id,))
        return run

    def list_runs(self, *, limit: int = 50) -> list[dict]:
        return self._fetchall("SELECT * FROM qd_cn_fundamental_sync_runs ORDER BY created_at DESC LIMIT %s", (max(1, min(limit, 200)),))

    def set_run_status(self, run_id: str, status: str, *, error: str = "") -> None:
        terminal = status in {"succeeded", "partial", "failed", "cancelled"}
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    f"""UPDATE qd_cn_fundamental_sync_runs SET status=%s,last_error=%s,updated_at=NOW(),
                        started_at=CASE WHEN %s='running' THEN COALESCE(started_at,NOW()) ELSE started_at END,
                        finished_at=CASE WHEN %s THEN NOW() ELSE finished_at END WHERE run_id=%s""",
                    (status, error[:1000], status, terminal, run_id),
                )
                db.commit()
            finally:
                cur.close()

    def set_target_status(self, run_id: str, instrument: str, status: str, *, checkpoint_period_end=None, error: str = "") -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """UPDATE qd_cn_fundamental_sync_targets SET status=%s,
                       checkpoint_period_end=COALESCE(%s,checkpoint_period_end),
                       attempts=attempts+CASE WHEN %s='running' THEN 1 ELSE 0 END,
                       last_error=%s,updated_at=NOW(),
                       started_at=CASE WHEN %s='running' THEN COALESCE(started_at,NOW()) ELSE started_at END,
                       finished_at=CASE WHEN %s IN ('succeeded','failed','cancelled') THEN NOW() ELSE finished_at END
                       WHERE run_id=%s AND instrument=%s""",
                    (status, checkpoint_period_end, status, error[:1000], status, status, run_id, instrument),
                )
                db.commit()
            finally:
                cur.close()

    def refresh_run_progress(self, run_id: str) -> dict:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """SELECT COUNT(*) FILTER (WHERE status='succeeded') succeeded,
                              COUNT(*) FILTER (WHERE status='failed') failed,
                              COUNT(*) FILTER (WHERE status='cancelled') cancelled
                       FROM qd_cn_fundamental_sync_targets WHERE run_id=%s""",
                    (run_id,),
                )
                counts = dict(cur.fetchone() or {})
                cur.execute(
                    """UPDATE qd_cn_fundamental_sync_runs SET succeeded_symbols=%s,
                              failed_symbols=%s,skipped_symbols=%s,updated_at=NOW()
                       WHERE run_id=%s""",
                    (counts.get("succeeded", 0), counts.get("failed", 0), counts.get("cancelled", 0), run_id),
                )
                db.commit()
                return counts
            finally:
                cur.close()

    def write_audit(self, actor_user_id: int | None, action: str, *, run_id: str | None = None, details: Mapping | None = None) -> None:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute("INSERT INTO qd_cn_fundamental_operation_audit (actor_user_id,action,run_id,details) VALUES (%s,%s,%s,%s::jsonb)", (actor_user_id, action, run_id, _json(details)))
                db.commit()
            finally:
                cur.close()

    def _fetchall(self, sql: str, params: tuple) -> list[dict]:
        with self._connection_factory() as db:
            cur = db.cursor()
            try:
                cur.execute(sql, params)
                return cur.fetchall()
            finally:
                cur.close()
