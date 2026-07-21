"""Quality checks and fail-closed coverage assessment for A-share history."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from .calendar import expected_a_share_sessions
from .models import (
    AdjustmentMode,
    CorporateAction,
    CoverageGap,
    CoverageReport,
    QualityFinding,
    QualitySeverity,
    RawDailyBar,
)
from .operations_repository import CNMarketHistoryOperationsRepository
from .repository import CNMarketHistoryRepository


class CNHistoryQualityError(RuntimeError):
    code = "cn_history.quality_blocked"


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    report: CoverageReport
    findings: tuple[QualityFinding, ...]


def finding_fingerprint(finding: QualityFinding) -> str:
    identity = "|".join(
        (
            finding.instrument,
            finding.finding_type,
            finding.start_date.isoformat(),
            finding.end_date.isoformat(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _group_gaps(dates: Sequence[date], expected: Sequence[date]) -> tuple[CoverageGap, ...]:
    if not dates:
        return ()
    positions = {session: index for index, session in enumerate(expected)}
    groups: list[list[date]] = [[dates[0]]]
    for item in dates[1:]:
        if positions[item] == positions[groups[-1][-1]] + 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    return tuple(
        CoverageGap(
            start_date=group[0],
            end_date=group[-1],
            reason="missing_daily_bar",
            dates=tuple(group),
        )
        for group in groups
    )


class CNMarketHistoryQualityService:
    def __init__(
        self,
        data_repository: CNMarketHistoryRepository | None = None,
        operations_repository: CNMarketHistoryOperationsRepository | None = None,
    ) -> None:
        self.data_repository = data_repository or CNMarketHistoryRepository()
        self.operations_repository = operations_repository or CNMarketHistoryOperationsRepository()

    def validate_bars(
        self,
        instrument: str,
        bars: Sequence[RawDailyBar],
        *,
        expected_sessions: set[date] | None = None,
    ) -> tuple[QualityFinding, ...]:
        findings: list[QualityFinding] = []
        seen: set[date] = set()
        for bar in sorted(bars, key=lambda item: item.trade_date):
            evidence = {
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "amount": str(bar.amount),
            }
            if bar.trade_date in seen:
                findings.append(
                    QualityFinding(
                        instrument=instrument,
                        finding_type="duplicate_trade_date",
                        severity=QualitySeverity.BLOCKING,
                        start_date=bar.trade_date,
                        end_date=bar.trade_date,
                        evidence=evidence,
                    )
                )
            seen.add(bar.trade_date)
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                finding_type = "non_positive_price"
            elif bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
                bar.open, bar.close, bar.high
            ):
                finding_type = "invalid_ohlc_relationship"
            elif bar.volume < 0 or bar.amount < 0:
                finding_type = "negative_volume_or_amount"
            else:
                finding_type = ""
            if finding_type:
                findings.append(
                    QualityFinding(
                        instrument=instrument,
                        finding_type=finding_type,
                        severity=QualitySeverity.BLOCKING,
                        start_date=bar.trade_date,
                        end_date=bar.trade_date,
                        evidence=evidence,
                    )
                )
            if expected_sessions is not None and bar.trade_date not in expected_sessions:
                findings.append(
                    QualityFinding(
                        instrument=instrument,
                        finding_type="outside_exchange_calendar",
                        severity=QualitySeverity.BLOCKING,
                        start_date=bar.trade_date,
                        end_date=bar.trade_date,
                        evidence=evidence,
                    )
                )
        return tuple(findings)

    def validate_actions(
        self,
        instrument: str,
        actions: Sequence[CorporateAction],
    ) -> tuple[QualityFinding, ...]:
        findings: list[QualityFinding] = []
        zero = Decimal("0")
        for action in actions:
            values = (
                action.cash_dividend,
                action.rights_price,
                action.bonus_ratio,
                action.rights_ratio,
                action.consolidation_ratio,
            )
            if any(value is not None and value < zero for value in values):
                reason = "negative_corporate_action_value"
            elif action.category == 1 and not any(value is not None and value > zero for value in values):
                reason = "incomplete_corporate_action"
            elif action.rights_ratio and action.rights_ratio > zero and action.rights_price is None:
                reason = "rights_issue_price_missing"
            elif action.category in {11, 12} and not (
                action.consolidation_ratio and action.consolidation_ratio > zero
            ):
                reason = "consolidation_ratio_missing"
            else:
                reason = ""
            if reason:
                findings.append(
                    QualityFinding(
                        instrument=instrument,
                        finding_type=reason,
                        severity=QualitySeverity.BLOCKING,
                        start_date=action.event_date,
                        end_date=action.event_date,
                        evidence={"category": action.category, "event_name": action.event_name},
                    )
                )
        return tuple(findings)

    def assess(
        self,
        instrument: str,
        start_date: date,
        end_date: date,
        *,
        provider: str = "easy_tdx",
        persist: bool = True,
    ) -> QualityAssessment:
        metadata = self.data_repository.get_instrument_metadata(instrument) or {}
        listed_on = metadata.get("listed_on")
        delisted_on = metadata.get("delisted_on")
        confirmed_non_trading = self.data_repository.fetch_confirmed_non_trading_dates(
            instrument, start_date, end_date
        )
        expected = expected_a_share_sessions(
            start_date,
            end_date,
            listed_on=listed_on,
            delisted_on=delisted_on,
            confirmed_non_trading_dates=confirmed_non_trading,
        )
        expected_set = set(expected)
        bars = self.data_repository.fetch_daily_bars(
            instrument, start_date, end_date, provider=provider
        )
        actions_rows = self.data_repository.fetch_corporate_actions(
            instrument, start_date, end_date, provider=provider
        )
        findings = list(self.validate_bars(instrument, bars, expected_sessions=expected_set))
        findings.extend(self._jump_findings(instrument, bars, actions_rows))
        actual_dates = {bar.trade_date for bar in bars}
        missing = sorted(expected_set - actual_dates)
        gaps = _group_gaps(missing, expected)
        for gap in gaps:
            findings.append(
                QualityFinding(
                    instrument=instrument,
                    finding_type="missing_daily_bar",
                    severity=QualitySeverity.BLOCKING,
                    start_date=gap.start_date,
                    end_date=gap.end_date,
                    evidence={"dates": [item.isoformat() for item in gap.dates]},
                )
            )
        blocking = sum(1 for finding in findings if finding.severity is QualitySeverity.BLOCKING)
        data_version = self.data_repository.get_data_version(
            instrument, start_date, end_date, provider=provider
        )
        report = CoverageReport(
            instrument=instrument,
            requested_start=start_date,
            requested_end=end_date,
            first_trade_date=min(actual_dates) if actual_dates else None,
            last_trade_date=max(actual_dates) if actual_dates else None,
            expected_sessions=len(expected),
            actual_sessions=len(actual_dates & expected_set),
            gaps=gaps,
            blocking_findings=blocking,
            complete=bool(expected) and not gaps and blocking == 0,
            data_version=data_version,
        )
        if persist:
            self.operations_repository.upsert_quality_findings(findings)
            self.operations_repository.resolve_absent_findings(
                instrument,
                [finding_fingerprint(finding) for finding in findings],
            )
            self.operations_repository.upsert_coverage(
                report,
                provider=provider,
                mode=AdjustmentMode.RAW,
            )
        return QualityAssessment(report=report, findings=tuple(findings))

    @staticmethod
    def _jump_findings(instrument: str, bars: Sequence[RawDailyBar], actions: Sequence[dict]):
        action_dates = {row["event_date"] for row in actions}
        findings = []
        ordered = sorted(bars, key=lambda item: item.trade_date)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.close <= 0 or current.trade_date in action_dates:
                continue
            change = abs(current.close / previous.close - Decimal("1"))
            if change > Decimal("0.35"):
                findings.append(
                    QualityFinding(
                        instrument=instrument,
                        finding_type="unexplained_price_jump",
                        severity=QualitySeverity.WARNING,
                        start_date=current.trade_date,
                        end_date=current.trade_date,
                        evidence={
                            "previous_close": str(previous.close),
                            "close": str(current.close),
                            "change": str(change),
                        },
                    )
                )
        return findings
