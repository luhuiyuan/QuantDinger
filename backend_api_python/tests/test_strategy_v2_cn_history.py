from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.services.cn_market_history.models import (
    AdjustmentMode,
    CoverageGap,
    CoverageReport,
    QualityFinding,
    QualitySeverity,
)
from app.services.cn_market_history.quality import QualityAssessment
from app.services.cn_market_history.query_service import HistoryQueryResult
from app.services.strategy_v2.market_data import (
    CNHistoryCoverageError,
    load_cn_strategy_frame,
)
from app.services.strategy_v2.service import StrategyV2BacktestService


class _QualityService:
    def __init__(self, assessments):
        self.assessments = list(assessments)
        self.calls = []

    def assess(self, instrument, start_date, end_date, *, provider, persist):
        self.calls.append((instrument, start_date, end_date, provider, persist))
        return self.assessments.pop(0)


class _QueryService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def load(self, instrument, start_date, end_date, *, mode, provider):
        self.calls.append((instrument, start_date, end_date, mode, provider))
        return self.result


def _assessment(start_date, end_date, *, complete=True, gaps=(), findings=()):
    report = CoverageReport(
        instrument="CNStock:600519.SH",
        requested_start=start_date,
        requested_end=end_date,
        first_trade_date=start_date if complete else None,
        last_trade_date=end_date if complete else None,
        expected_sessions=2,
        actual_sessions=2 if complete else 0,
        gaps=tuple(gaps),
        blocking_findings=sum(
            finding.severity is QualitySeverity.BLOCKING for finding in findings
        ),
        complete=complete,
        data_version="bars-v3",
    )
    return QualityAssessment(report=report, findings=tuple(findings))


def _query_result():
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 1100.0],
            "amount": [100000.0, 112200.0],
            "previous_close": [99.0, 101.0],
            "board_classification": ["main_board", "main_board"],
            "status_classification": ["non_st", "non_st"],
            "classification_confirmed": [True, True],
            "is_suspended": [False, False],
        },
        index=pd.DatetimeIndex(["2026-07-16", "2026-07-17"], name="date"),
    )
    return HistoryQueryResult(
        frame=frame,
        provenance={
            "instrument": "CNStock:600519.SH",
            "provider": "easy_tdx",
            "providerVersion": "1.20.4",
            "dataVersion": "bars-v3",
            "adjustmentMode": "raw",
            "factorVersion": None,
            "firstTradeDate": "2026-07-16",
            "lastTradeDate": "2026-07-17",
        },
    )


def test_cn_daily_loader_preserves_shanghai_session_dates_and_provenance():
    quality = _QualityService(
        [
            _assessment(date(2026, 7, 14), date(2026, 7, 15)),
            _assessment(date(2026, 7, 16), date(2026, 7, 17)),
        ]
    )
    query = _QueryService(_query_result())

    frame = load_cn_strategy_frame(
        "CNStock",
        "600519.SH",
        "1d",
        datetime(2026, 7, 14),
        datetime(2026, 7, 17, 23, 59),
        requested_start=datetime(2026, 7, 16),
        warmup_bars=2,
        quality_service=quality,
        query_service=query,
        completed_through=date(2026, 7, 17),
    )

    assert frame.index.tz is None
    assert [item.date().isoformat() for item in frame.index] == [
        "2026-07-16",
        "2026-07-17",
    ]
    assert frame.attrs["cn_history_provenance"]["provider"] == "easy_tdx"
    assert frame.attrs["cn_history_provenance"]["availableThrough"] == "2026-07-17"
    assert query.calls[0][3] is AdjustmentMode.RAW


def test_cn_daily_loader_uses_previous_trading_session_for_monday_warmup():
    quality = _QualityService(
        [
            _assessment(date(2025, 7, 18), date(2025, 7, 18)),
            _assessment(date(2025, 7, 21), date(2025, 7, 22)),
        ]
    )

    load_cn_strategy_frame(
        "CNStock",
        "600519.SH",
        "1d",
        datetime(2025, 7, 19),
        datetime(2025, 7, 22, 23, 59),
        requested_start=datetime(2025, 7, 21),
        warmup_bars=1,
        quality_service=quality,
        query_service=_QueryService(_query_result()),
        completed_through=date(2025, 7, 22),
    )

    assert quality.calls[0][1:3] == (date(2025, 7, 18), date(2025, 7, 18))


def test_cn_daily_loader_uses_sessions_before_exchange_holiday_for_warmup():
    quality = _QualityService(
        [
            _assessment(date(2025, 9, 29), date(2025, 9, 30)),
            _assessment(date(2025, 10, 9), date(2025, 10, 10)),
        ]
    )

    load_cn_strategy_frame(
        "CNStock",
        "600519.SH",
        "1d",
        datetime(2025, 10, 6),
        datetime(2025, 10, 10, 23, 59),
        requested_start=datetime(2025, 10, 9),
        warmup_bars=2,
        quality_service=quality,
        query_service=_QueryService(_query_result()),
        completed_through=date(2025, 10, 10),
    )

    assert quality.calls[0][1:3] == (date(2025, 9, 29), date(2025, 9, 30))


def test_cn_daily_loader_reports_separate_warmup_coverage_error():
    gap = CoverageGap(
        start_date=date(2026, 7, 15),
        end_date=date(2026, 7, 15),
        reason="missing_daily_bar",
        dates=(date(2026, 7, 15),),
    )
    finding = QualityFinding(
        instrument="CNStock:600519.SH",
        finding_type="missing_daily_bar",
        severity=QualitySeverity.BLOCKING,
        start_date=date(2026, 7, 15),
        end_date=date(2026, 7, 15),
    )
    quality = _QualityService(
        [
            _assessment(
                date(2026, 7, 14),
                date(2026, 7, 15),
                complete=False,
                gaps=(gap,),
                findings=(finding,),
            ),
            _assessment(date(2026, 7, 16), date(2026, 7, 17)),
        ]
    )

    with pytest.raises(CNHistoryCoverageError) as caught:
        load_cn_strategy_frame(
            "CNStock",
            "600519.SH",
            "1d",
            datetime(2026, 7, 14),
            datetime(2026, 7, 17, 23, 59),
            requested_start=datetime(2026, 7, 16),
            warmup_bars=2,
            quality_service=quality,
            query_service=_QueryService(_query_result()),
            completed_through=date(2026, 7, 17),
        )

    details = caught.value.details
    assert details["code"] == "strategyV2.cnHistoryCoverageIncomplete"
    assert details["instruments"][0]["issues"][0]["scope"] == "warmup"
    assert details["instruments"][0]["suggestedAction"]["type"] == "admin_targeted_sync"


def test_cn_daily_loader_truncates_unfinished_session_explicitly():
    quality = _QualityService(
        [_assessment(date(2026, 7, 16), date(2026, 7, 17))]
    )
    frame = load_cn_strategy_frame(
        "CNStock",
        "600519.SH",
        "1d",
        datetime(2026, 7, 16),
        datetime(2026, 7, 20, 23, 59),
        requested_start=datetime(2026, 7, 16),
        warmup_bars=0,
        quality_service=quality,
        query_service=_QueryService(_query_result()),
        completed_through=date(2026, 7, 17),
    )

    provenance = frame.attrs["cn_history_provenance"]
    assert provenance["requestedEnd"] == "2026-07-20"
    assert provenance["availableThrough"] == "2026-07-17"
    assert provenance["truncatedUnfinishedSession"] is True


def test_cn_daily_loader_fails_before_execution_when_rule_history_is_missing():
    result = _query_result()
    result.frame.loc[:, "classification_confirmed"] = False
    quality = _QualityService(
        [_assessment(date(2026, 7, 16), date(2026, 7, 17))]
    )

    with pytest.raises(CNHistoryCoverageError) as caught:
        load_cn_strategy_frame(
            "CNStock",
            "600519.SH",
            "1d",
            datetime(2026, 7, 16),
            datetime(2026, 7, 17, 23, 59),
            requested_start=datetime(2026, 7, 16),
            warmup_bars=0,
            quality_service=quality,
            query_service=_QueryService(result),
            completed_through=date(2026, 7, 17),
        )

    issue = caught.value.details["instruments"][0]["issues"][0]
    assert issue["scope"] == "market_rules"
    assert issue["type"] == "market_rule_data_incomplete"


def test_service_does_not_call_remote_or_run_partial_cn_portfolio_on_gap():
    remote_calls = []
    local_calls = []

    def remote_fetcher(*args, **kwargs):
        remote_calls.append((args, kwargs))
        return _query_result().frame

    def local_fetcher(_market, symbol, *_args, **_kwargs):
        local_calls.append(symbol)
        if symbol == "000001.SZ":
            raise CNHistoryCoverageError.for_instrument(
                instrument="CNStock:000001.SZ",
                requested_start=date(2026, 7, 16),
                requested_end=date(2026, 7, 17),
                warmup_start=date(2026, 7, 14),
                warmup_end=date(2026, 7, 15),
                available_through=date(2026, 7, 17),
                adjustment_mode=AdjustmentMode.RAW,
                issues=[{"scope": "requested", "type": "missing_daily_bar"}],
            )
        return _query_result().frame

    service = StrategyV2BacktestService(
        frame_fetcher=remote_fetcher,
        cn_frame_fetcher=local_fetcher,
        cn_history_enabled=True,
    )
    candidates = [
        {"key": "CNStock:600519.SH", "market": "CNStock", "symbol": "600519.SH"},
        {"key": "CNStock:000001.SZ", "market": "CNStock", "symbol": "000001.SZ"},
    ]

    with pytest.raises(CNHistoryCoverageError) as caught:
        service.fetch_frames(
            candidates,
            "1d",
            datetime(2026, 7, 14),
            datetime(2026, 7, 17, 23, 59),
            requested_start=datetime(2026, 7, 16),
            warmup_bars=2,
        )

    assert local_calls == ["600519.SH", "000001.SZ"]
    assert remote_calls == []
    assert len(caught.value.details["instruments"]) == 1


def test_service_coverage_failure_includes_cn_execution_assumptions():
    def local_fetcher(*_args, **_kwargs):
        raise CNHistoryCoverageError.for_instrument(
            instrument="CNStock:600519.SH",
            requested_start=date(2026, 7, 16),
            requested_end=date(2026, 7, 17),
            warmup_start=None,
            warmup_end=None,
            available_through=date(2026, 7, 17),
            adjustment_mode=AdjustmentMode.RAW,
            issues=[{"scope": "requested", "type": "missing_daily_bar"}],
        )

    code = """
def initialize(context):
    context.set_universe(["CNStock:600519.SH"])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    pass
"""
    service = StrategyV2BacktestService(
        cn_frame_fetcher=local_fetcher,
        cn_history_enabled=True,
    )

    with pytest.raises(CNHistoryCoverageError) as caught:
        service.run(
            user_id=1,
            code=code,
            start_date=datetime(2026, 7, 16),
            end_date=datetime(2026, 7, 17, 23, 59),
            initial_capital=10000,
            commission=0.0004,
            slippage=0.0002,
            persist=False,
        )

    assumptions = caught.value.details["executionAssumptions"]
    assert assumptions["marketRuleVersion"] == "cn-stock-rules-2026.1"
    assert assumptions["commissionRate"] == 0.0004
    assert assumptions["slippage"] == 0.0002
