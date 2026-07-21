"""Sequential, checkpointed synchronization of durable A-share history."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Sequence

from app.utils.logger import get_logger

from .config import CNMarketHistorySettings, load_cn_market_history_settings
from .disk_guard import DiskGuard, DiskGuardStatus
from .instruments import parse_cn_instrument
from .locks import cn_history_advisory_lock
from .models import AdjustmentMode, ProviderProbe, SyncStatus, SyncTarget
from .official_adjustments import (
    OfficialAdjustmentReferenceProvider,
    relevant_corporate_actions,
    verify_corporate_action_references,
)
from .operations_repository import CNMarketHistoryOperationsRepository
from .repository import CNMarketHistoryRepository
from .tdx_provider import PROVIDER_NAME, TDXProvider, TDXProviderError
from .quality import CNHistoryQualityError, CNMarketHistoryQualityService
from .adjustments import calculate_adjustment_factors

logger = get_logger(__name__)


class CNHistorySyncError(RuntimeError):
    code = "cn_history.sync_error"


class CNHistorySyncDisabled(CNHistorySyncError):
    code = "cn_history.sync_disabled"


class CNHistoryDiskBlocked(CNHistorySyncError):
    code = "cn_history.disk_soft_limit"

    def __init__(self, status: DiskGuardStatus) -> None:
        self.status = status
        super().__init__(f"CN history sync blocked with {status.free_bytes} free bytes")


class CNHistoryDuplicateRun(CNHistorySyncError):
    code = "cn_history.sync_overlap"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Overlapping CN history sync run already active: {run_id}")


class CNMarketHistorySyncService:
    def __init__(
        self,
        *,
        settings: CNMarketHistorySettings | None = None,
        data_repository: CNMarketHistoryRepository | None = None,
        operations_repository: CNMarketHistoryOperationsRepository | None = None,
        provider_factory=TDXProvider,
        official_provider_factory=OfficialAdjustmentReferenceProvider,
        disk_guard: DiskGuard | None = None,
        lock_factory=cn_history_advisory_lock,
        quality_service: CNMarketHistoryQualityService | None = None,
    ) -> None:
        self.settings = settings or load_cn_market_history_settings()
        self.data_repository = data_repository or CNMarketHistoryRepository()
        self.operations_repository = operations_repository or CNMarketHistoryOperationsRepository()
        self.provider_factory = provider_factory
        self.official_provider = official_provider_factory(self.settings)
        self.disk_guard = disk_guard or DiskGuard(self.settings)
        self.lock_factory = lock_factory
        self.quality_service = quality_service or CNMarketHistoryQualityService(
            self.data_repository,
            self.operations_repository,
        )

    def create_targeted_run(
        self,
        instruments: Sequence[str],
        start_date: date,
        end_date: date,
        *,
        requested_by: int | None,
        parent_run_id: str | None = None,
        request_kind: str = "targeted",
    ) -> str:
        if not self.settings.sync_enabled:
            raise CNHistorySyncDisabled("CN history synchronization is disabled")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        canonical = []
        seen = set()
        for raw in instruments:
            instrument = parse_cn_instrument(raw)
            if instrument.canonical in seen:
                continue
            seen.add(instrument.canonical)
            canonical.append(instrument)
        if not canonical:
            raise ValueError("At least one supported A-share instrument is required")
        if len(canonical) > self.settings.max_targets_per_run:
            raise ValueError(
                f"A sync run accepts at most {self.settings.max_targets_per_run} instruments"
            )

        find_overlap = getattr(
            self.operations_repository, "find_overlapping_active_run", None
        )
        if callable(find_overlap):
            overlap = find_overlap(
                [item.canonical for item in canonical],
                start_date,
                end_date,
                exclude_run_id=parent_run_id,
            )
            if overlap:
                raise CNHistoryDuplicateRun(str(overlap["run_id"]))

        disk_status = self.disk_guard.check()
        if not disk_status.allows_new_sync:
            raise CNHistoryDiskBlocked(disk_status)
        targets = [
            SyncTarget(instrument=instrument, start_date=start_date, end_date=end_date)
            for instrument in canonical
        ]
        run_id = self.operations_repository.create_sync_run(
            targets,
            requested_by=requested_by,
            request_kind=request_kind,
            parent_run_id=parent_run_id,
            request_payload={
                "instruments": [target.instrument.canonical for target in targets],
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        audit_action = {
            "retry": "retry_sync",
            "repair": "repair_sync",
        }.get(request_kind, "create_sync")
        self.operations_repository.write_audit(
            actor_user_id=requested_by,
            action=audit_action,
            run_id=run_id,
            request_scope={
                "instrument_count": len(targets),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "request_kind": request_kind,
                "parent_run_id": parent_run_id,
            },
            result_status="pending",
        )
        return run_id

    def retry_failed_run(self, run_id: str, *, requested_by: int | None) -> str:
        run = self.operations_repository.get_sync_run(run_id)
        if not run:
            raise KeyError(f"Unknown CN history sync run: {run_id}")
        failed = [
            target
            for target in run.get("targets", [])
            if target.get("status") in {SyncStatus.FAILED.value, SyncStatus.PAUSED.value}
        ]
        if not failed:
            raise ValueError("The sync run has no failed or paused targets")
        return self.create_targeted_run(
            [target["instrument"] for target in failed],
            min(target["target_start"] for target in failed),
            max(target["target_end"] for target in failed),
            requested_by=requested_by,
            parent_run_id=run_id,
            request_kind="retry",
        )

    def cancel_run(self, run_id: str, *, requested_by: int | None) -> None:
        run = self.operations_repository.get_sync_run(run_id)
        if not run:
            raise KeyError(f"Unknown CN history sync run: {run_id}")
        if run["status"] in {
            SyncStatus.SUCCEEDED.value,
            SyncStatus.FAILED.value,
            SyncStatus.CANCELLED.value,
        }:
            raise ValueError("The sync run is already terminal")
        self.operations_repository.update_run(run_id, SyncStatus.CANCELLED)
        self.operations_repository.write_audit(
            actor_user_id=requested_by,
            action="cancel_sync",
            run_id=run_id,
            request_scope={
                "instrument_count": len(run.get("targets") or []),
                "start_date": str(run.get("target_start") or ""),
                "end_date": str(run.get("target_end") or ""),
            },
            result_status="cancelled",
        )

    def run(self, run_id: str) -> dict:
        if not self.settings.sync_enabled:
            raise CNHistorySyncDisabled("CN history synchronization is disabled")
        run = self.operations_repository.get_sync_run(run_id)
        if not run:
            raise KeyError(f"Unknown CN history sync run: {run_id}")
        if run["status"] == SyncStatus.CANCELLED.value:
            return {"run_id": run_id, "status": SyncStatus.CANCELLED.value}

        targets = [
            target
            for target in run.get("targets", [])
            if target.get("status")
            in {
                SyncStatus.PENDING.value,
                SyncStatus.FAILED.value,
                SyncStatus.PAUSED.value,
            }
        ]
        with self.lock_factory("cn-history:global-sync") as acquired:
            if not acquired:
                self.operations_repository.update_run(
                    run_id,
                    SyncStatus.PAUSED,
                    last_error_code="cn_history.global_lock_busy",
                    last_error="Another CN history synchronization run is active",
                )
                return {"run_id": run_id, "status": SyncStatus.PAUSED.value, "lock_busy": True}
            return self._run_serialized(run_id, targets)

    def _run_serialized(self, run_id: str, targets: list[dict]) -> dict:
        self.operations_repository.update_run(run_id, SyncStatus.RUNNING)
        counts = {"succeeded": 0, "failed": 0, "skipped": 0, "paused": 0}

        provider = self.provider_factory(self.settings)
        try:
            probes = provider.probe_hosts()
            for probe in probes:
                self.operations_repository.upsert_provider_probe(
                    PROVIDER_NAME,
                    probe,
                    selected=probe.host == provider.selected_host,
                )
            with provider:
                for target in targets:
                    current = self.operations_repository.get_sync_run(run_id)
                    if current and current["status"] == SyncStatus.CANCELLED.value:
                        break
                    result = self._run_target(provider, run_id, target)
                    counts[result] += 1
        except TDXProviderError as exc:
            logger.warning("CN history provider unavailable for run %s: %s", run_id, exc)
            counts["failed"] += max(0, len(targets) - sum(counts.values()))
            self._record_selected_provider_failure(provider, exc)
        except Exception as exc:
            logger.exception("CN history sync run %s failed", run_id)
            counts["failed"] += max(0, len(targets) - sum(counts.values()))
            self.operations_repository.update_run(
                run_id,
                SyncStatus.FAILED,
                last_error_code="cn_history.sync_unexpected",
                last_error=str(exc),
            )
            raise

        final_status = self._final_status(counts)
        summary = {**counts, "run_id": run_id, "status": final_status.value}
        self.operations_repository.update_run(
            run_id,
            final_status,
            succeeded_symbols=counts["succeeded"],
            failed_symbols=counts["failed"],
            skipped_symbols=counts["skipped"],
            result_summary=summary,
        )
        return summary

    def _run_target(self, provider: TDXProvider, run_id: str, target: dict) -> str:
        instrument = parse_cn_instrument(target["instrument"])
        lock_key = f"cn-history:{instrument.canonical}"
        with self.lock_factory(lock_key) as acquired:
            if not acquired:
                self.operations_repository.update_target(
                    run_id,
                    instrument.canonical,
                    SyncStatus.SKIPPED,
                    last_error_code="cn_history.lock_busy",
                    last_error="Another worker owns this instrument sync",
                )
                return "skipped"

            attempts = int(target.get("attempts") or 0) + 1
            offset = int(target.get("page_offset") or 0)
            bars_written = int(target.get("bars_written") or 0)
            actions_written = int(target.get("actions_written") or 0)
            self.operations_repository.update_run(
                run_id,
                SyncStatus.RUNNING,
                current_instrument=instrument.canonical,
            )
            self.operations_repository.update_target(
                run_id,
                instrument.canonical,
                SyncStatus.RUNNING,
                attempts=attempts,
            )
            try:
                metadata, classifications = provider.fetch_instrument_metadata(instrument)
                self.data_repository.upsert_instrument_metadata(metadata, classifications)
                for page in provider.iter_daily_pages(
                    instrument,
                    target["target_start"],
                    target["target_end"],
                    start_offset=offset,
                ):
                    disk_status = self.disk_guard.check()
                    if not disk_status.allows_current_write:
                        self.operations_repository.update_target(
                            run_id,
                            instrument.canonical,
                            SyncStatus.PAUSED,
                            page_offset=page.offset,
                            bars_written=bars_written,
                            actions_written=actions_written,
                            last_error_code="cn_history.disk_hard_limit",
                            last_error=f"Only {disk_status.free_bytes} free bytes remain",
                        )
                        return "paused"
                    if page.bars:
                        page_findings = self.quality_service.validate_bars(
                            instrument.canonical,
                            page.bars,
                        )
                        if any(finding.severity.value == "blocking" for finding in page_findings):
                            self.operations_repository.upsert_quality_findings(page_findings)
                            raise CNHistoryQualityError("TDX page failed bar-quality checks")
                        summary = self.data_repository.upsert_daily_bars(page.bars)
                        bars_written += summary.written
                    offset = page.next_offset
                    checkpoint_date = min(
                        (bar.trade_date for bar in page.bars),
                        default=target.get("checkpoint_date"),
                    )
                    self.operations_repository.update_target(
                        run_id,
                        instrument.canonical,
                        SyncStatus.RUNNING,
                        page_offset=offset,
                        checkpoint_date=checkpoint_date,
                        bars_written=bars_written,
                        actions_written=actions_written,
                    )
                    if page.raw_count == 0 and bars_written == 0:
                        raise CNHistorySyncError("TDX returned no daily history")

                actions = provider.fetch_corporate_actions(instrument)
                stored_bars = self.data_repository.fetch_daily_bars(
                    instrument.canonical,
                    target["target_start"],
                    target["target_end"],
                    provider=PROVIDER_NAME,
                )
                relevant_actions = relevant_corporate_actions(stored_bars, actions)
                if relevant_actions:
                    references = self.official_provider.fetch_references(
                        instrument,
                        relevant_actions,
                    )
                    actions = list(verify_corporate_action_references(
                        stored_bars,
                        relevant_actions,
                        references,
                    ))
                else:
                    actions = []
                if actions:
                    action_findings = self.quality_service.validate_actions(
                        instrument.canonical,
                        actions,
                    )
                    if action_findings:
                        self.operations_repository.upsert_quality_findings(action_findings)
                        raise CNHistoryQualityError("Corporate-action history is incomplete")
                    action_summary = self.data_repository.upsert_corporate_actions(actions)
                    actions_written += action_summary.written
                    if action_summary.revised:
                        self.data_repository.invalidate_factor_versions(
                            instrument.canonical,
                            "corporate_action_revised",
                        )
                assessment = self.quality_service.assess(
                    instrument.canonical,
                    target["target_start"],
                    target["target_end"],
                    provider=PROVIDER_NAME,
                )
                if assessment.report.complete:
                    relevant_actions = relevant_corporate_actions(stored_bars, actions)
                    action_digest = hashlib.sha256()
                    for action in relevant_actions:
                        action_digest.update(f"{action.content_hash}\n".encode("ascii"))
                    action_data_version = action_digest.hexdigest()
                    for mode in (AdjustmentMode.FORWARD, AdjustmentMode.BACKWARD):
                        factors = calculate_adjustment_factors(
                            stored_bars,
                            relevant_actions,
                            mode,
                        )
                        if factors:
                            self.data_repository.store_adjustment_factors(
                                factors,
                                action_data_version=action_data_version,
                            )
                            self.operations_repository.upsert_coverage(
                                assessment.report,
                                provider=PROVIDER_NAME,
                                mode=mode,
                                factor_version=factors[0].factor_version,
                                last_successful_sync_at=datetime.now(timezone.utc),
                            )
                self.operations_repository.update_target(
                    run_id,
                    instrument.canonical,
                    SyncStatus.SUCCEEDED,
                    page_offset=offset,
                    bars_written=bars_written,
                    actions_written=actions_written,
                    last_error_code="",
                    last_error="",
                )
                return "succeeded"
            except Exception as exc:
                error_code = getattr(exc, "code", "cn_history.target_failed")
                self.operations_repository.update_target(
                    run_id,
                    instrument.canonical,
                    SyncStatus.FAILED,
                    page_offset=offset,
                    attempts=attempts,
                    bars_written=bars_written,
                    actions_written=actions_written,
                    last_error_code=error_code,
                    last_error=str(exc),
                )
                logger.warning("CN history target %s failed: %s", instrument.canonical, exc)
                return "failed"

    def _record_selected_provider_failure(self, provider: TDXProvider, exc: Exception) -> None:
        if not provider.selected_host:
            return
        now = datetime.now(timezone.utc)
        self.operations_repository.upsert_provider_probe(
            PROVIDER_NAME,
            ProviderProbe(
                host=provider.selected_host,
                latency_ms=None,
                healthy=False,
                checked_at=now,
                consecutive_failures=1,
                error_code=getattr(exc, "code", "provider_error"),
            ),
            selected=True,
        )

    @staticmethod
    def _final_status(counts: dict[str, int]) -> SyncStatus:
        if counts["paused"]:
            return SyncStatus.PAUSED
        if counts["failed"] and counts["succeeded"]:
            return SyncStatus.PARTIAL
        if counts["failed"]:
            return SyncStatus.FAILED
        return SyncStatus.SUCCEEDED
