"""Application services for annual fundamental ingestion and controlled runs."""

from __future__ import annotations

from datetime import date

from app.data_sources.cn_fundamental_history import fetch_eastmoney_annual_reports
from app.services.cn_market_history.disk_guard import DiskGuard
from app.services.cn_market_history.config import load_cn_market_history_settings

from .industry import is_interest_coverage_exempt
from .metrics import FORMULA_VERSION, FundamentalMetricCalculator
from .quality import reconcile_values, screen_metrics
from .repository import CNFundamentalHistoryRepository

REQUIRED_FIELDS = (
    "parent_net_income", "parent_equity_begin", "parent_equity_end",
    "operating_cash_flow", "capex_cash_paid", "ebit", "interest_expense",
    "revenue", "gross_profit", "total_shares",
)


class CNFundamentalHistoryService:
    def __init__(self, repository=None, fetcher=fetch_eastmoney_annual_reports, calculator=None):
        self.repository = repository or CNFundamentalHistoryRepository()
        self.fetcher = fetcher
        self.calculator = calculator or FundamentalMetricCalculator()

    def sync_instrument(self, instrument: str, *, as_of: date | None = None) -> dict:
        as_of = as_of or date.today()
        code = instrument.split(":")[-1].split(".")[0]
        rows = self.fetcher(code)
        written = []
        for row in rows:
            if row["available_at"] > as_of:
                continue
            written.append(self.repository.append_observation(
                instrument=instrument,
                period_end=row["period_end"],
                available_at=row["available_at"],
                source="eastmoney",
                raw_payload=row["raw"],
                fields=row["fields"],
                source_version=row["source_version"],
                request_context=row.get("request_context"),
                announcement_ref=row["announcement_ref"],
                mapping_version=row["mapping_version"],
            ))
        observations = self.repository.load_as_of(instrument, as_of)
        if observations:
            classification = self.repository.load_industry_as_of(instrument, as_of)
            observations[-1]["industry_exempt_interest_coverage"] = is_interest_coverage_exempt(classification)
        metrics = self.calculator.calculate(observations)
        if observations:
            latest = observations[-1]
            self.repository.upsert_metrics(instrument, latest["period_end"], latest["available_at"], metrics, formula_version=FORMULA_VERSION)
        coverage = self.repository.refresh_coverage(instrument, required_fields=REQUIRED_FIELDS)
        screening = screen_metrics(metrics)
        if observations:
            self.repository.persist_screening(instrument, latest["period_end"], latest["available_at"], screening, formula_version=FORMULA_VERSION)
        return {
            "instrument": instrument,
            "observationsWritten": len(written),
            "metrics": metrics,
            "screening": screening,
            "coverage": coverage,
        }


class CNFundamentalVerificationService:
    def __init__(self, repository=None):
        self.repository = repository or CNFundamentalHistoryRepository()

    def reconcile_target(self, *, target_id: int, instrument: str, period_end: date, primary_fields, official_fields) -> dict:
        field_codes = sorted(set(primary_fields) | set(official_fields))
        results = {}
        for field_code in field_codes:
            result = reconcile_values(primary_fields.get(field_code), official_fields.get(field_code), evidence={"fieldCode": field_code})
            self.repository.record_reconciliation(target_id, field_code, result, instrument=instrument, period_end=period_end)
            results[field_code] = result
        return {"targetId": target_id, "status": self.repository.finalize_verification_target(target_id), "fields": results}


class CNFundamentalRunService:
    """Serialized, checkpointed executor shared by admin and scheduled tasks."""

    def __init__(self, repository=None, history_service=None, disk_guard=None):
        self.repository = repository or CNFundamentalHistoryRepository()
        self.history_service = history_service or CNFundamentalHistoryService(repository=self.repository)
        self.disk_guard = disk_guard or DiskGuard(load_cn_market_history_settings())

    def create_run(self, instruments=None, *, requested_by: int | None, request_kind: str = "backfill", full_market: bool = False) -> str:
        instruments = self.repository.list_eligible_instruments() if full_market else (instruments or ())
        unique = tuple(dict.fromkeys(str(item).strip() for item in instruments if str(item).strip()))
        if not unique:
            raise ValueError("cn_fundamental.instruments_required")
        run_id = self.repository.create_run(unique, requested_by=requested_by, request_kind=request_kind, request_payload={"instruments": unique})
        self.repository.write_audit(requested_by, "create", run_id=run_id, details={"requestKind": request_kind, "targetCount": len(unique)})
        return run_id

    def set_control_status(self, run_id: str, status: str, *, actor_user_id: int | None) -> None:
        if status not in {"paused", "pending", "cancelled"}:
            raise ValueError("cn_fundamental.invalid_control_status")
        if not self.repository.get_run(run_id):
            raise KeyError(run_id)
        self.repository.set_run_status(run_id, status)
        self.repository.write_audit(actor_user_id, status, run_id=run_id)

    def retry_failed(self, run_id: str, *, actor_user_id: int | None) -> str:
        run = self.repository.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        failed = [item["instrument"] for item in run["targets"] if item["status"] in {"failed", "pending"}]
        return self.create_run(failed, requested_by=actor_user_id, request_kind="retry")

    def run(self, run_id: str) -> dict:
        run = self.repository.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        with self.repository.advisory_lock() as acquired:
            if not acquired:
                return {"runId": run_id, "skipped": True, "reason": "active_fundamental_run"}
            self.repository.set_run_status(run_id, "running")
            succeeded = failed = 0
            for target in run["targets"]:
                if target["status"] == "succeeded":
                    succeeded += 1
                    continue
                current = self.repository.get_run(run_id)
                if current["status"] in {"paused", "cancelled"}:
                    break
                instrument = target["instrument"]
                try:
                    if self.disk_guard is not None:
                        status = self.disk_guard.check()
                        if not status.allows_current_write:
                            raise RuntimeError("cn_fundamental.disk_write_blocked")
                    self.repository.set_target_status(run_id, instrument, "running")
                    result = self.history_service.sync_instrument(instrument)
                    checkpoint = result["coverage"].get("last_period_end")
                    self.repository.set_target_status(run_id, instrument, "succeeded", checkpoint_period_end=checkpoint)
                    succeeded += 1
                except Exception as exc:
                    self.repository.set_target_status(run_id, instrument, "failed", error=str(exc))
                    failed += 1
                self.repository.refresh_run_progress(run_id)
            status = "partial" if failed and succeeded else "failed" if failed else "succeeded"
            latest = self.repository.get_run(run_id)
            if latest["status"] not in {"paused", "cancelled"}:
                self.repository.set_run_status(run_id, status)
            return {"runId": run_id, "status": status, "succeeded": succeeded, "failed": failed}
