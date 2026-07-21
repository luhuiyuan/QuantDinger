from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from app.services.cn_market_history.adjustments import calculate_adjustment_factors
from app.services.cn_market_history.instruments import parse_cn_instrument
from app.services.cn_market_history.models import (
    AdjustmentMode,
    CorporateAction,
    RawDailyBar,
)
from app.services.cn_market_history.quality import CNMarketHistoryQualityService
from app.services.cn_market_history.query_service import CNMarketHistoryQueryService


def _bar(day: int, close: str) -> RawDailyBar:
    instrument = parse_cn_instrument("600519")
    value = Decimal(close)
    return RawDailyBar(
        instrument=instrument,
        trade_date=date(2024, 1, day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1000"),
        amount=Decimal("10000"),
        provider="easy_tdx",
        provider_version="1.20.4",
        content_hash=f"bar-{day}-{close}",
        collected_at=datetime.now(timezone.utc),
    )


def test_adjustment_factors_are_deterministic_and_continuous() -> None:
    instrument = parse_cn_instrument("600519")
    bars = (_bar(2, "10"), _bar(3, "9"), _bar(4, "9.5"))
    actions = (
        CorporateAction(
            instrument=instrument,
            event_date=date(2024, 1, 3),
            category=1,
            event_name="cash dividend",
            cash_dividend=Decimal("1"),
            provider_version="1.20.4",
            content_hash="action-1",
        ),
    )

    forward = calculate_adjustment_factors(bars, actions, AdjustmentMode.FORWARD)
    repeated = calculate_adjustment_factors(bars, actions, AdjustmentMode.FORWARD)
    backward = calculate_adjustment_factors(bars, actions, AdjustmentMode.BACKWARD)

    assert [factor.factor for factor in forward] == [Decimal("0.9"), Decimal("1"), Decimal("1")]
    assert forward[0].factor_version == repeated[0].factor_version
    assert forward[0].anchor_date == date(2024, 1, 4)
    assert backward[0].factor == Decimal("1")
    assert backward[1].factor == Decimal("1") / Decimal("0.9")
    assert backward[0].anchor_date == date(2024, 1, 2)


class _QualityRepository:
    def __init__(self, *, suspended=False) -> None:
        self.suspended = suspended
        self.bars = [_bar(2, "10"), _bar(4, "10.2")]

    def get_instrument_metadata(self, instrument):
        del instrument
        return {"listed_on": date(2024, 1, 2), "delisted_on": None}

    def fetch_confirmed_non_trading_dates(self, instrument, start_date, end_date):
        del instrument, start_date, end_date
        return {date(2024, 1, 3)} if self.suspended else set()

    def fetch_daily_bars(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return self.bars

    def fetch_corporate_actions(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return []

    def get_data_version(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return "data-version"


class _QualityOperations:
    def __init__(self) -> None:
        self.coverage = None
        self.findings = []

    def upsert_quality_findings(self, findings):
        self.findings = list(findings)

    def resolve_absent_findings(self, instrument, fingerprints):
        del instrument, fingerprints

    def upsert_coverage(self, report, **kwargs):
        self.coverage = (report, kwargs)


def test_coverage_fails_closed_for_unexplained_session_gap() -> None:
    repository = _QualityRepository(suspended=False)
    operations = _QualityOperations()
    service = CNMarketHistoryQualityService(repository, operations)

    assessment = service.assess(
        "CNStock:600519.SH",
        date(2024, 1, 2),
        date(2024, 1, 4),
    )

    assert assessment.report.complete is False
    assert assessment.report.expected_sessions == 3
    assert assessment.report.actual_sessions == 2
    assert assessment.report.gaps[0].dates == (date(2024, 1, 3),)
    assert any(finding.finding_type == "missing_daily_bar" for finding in assessment.findings)


def test_confirmed_suspension_is_not_treated_as_missing_data() -> None:
    repository = _QualityRepository(suspended=True)
    operations = _QualityOperations()
    service = CNMarketHistoryQualityService(repository, operations)

    assessment = service.assess(
        "CNStock:600519.SH",
        date(2024, 1, 2),
        date(2024, 1, 4),
    )

    assert assessment.report.complete is True
    assert assessment.report.expected_sessions == 2
    assert assessment.report.actual_sessions == 2
    assert assessment.report.gaps == ()


def test_incomplete_corporate_action_is_blocking() -> None:
    instrument = parse_cn_instrument("600519")
    service = CNMarketHistoryQualityService(_QualityRepository(), _QualityOperations())
    findings = service.validate_actions(
        instrument.canonical,
        [
            CorporateAction(
                instrument=instrument,
                event_date=date(2024, 1, 3),
                category=1,
                event_name="incomplete",
            )
        ],
    )

    assert findings[0].finding_type == "incomplete_corporate_action"
    assert findings[0].severity.value == "blocking"


class _QueryRepository:
    def fetch_daily_bars(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return [_bar(2, "10"), _bar(3, "9")]

    def fetch_active_factors(self, instrument, mode, start_date, end_date):
        del instrument, mode, start_date, end_date
        return "factor-version", [
            {"trade_date": date(2024, 1, 2), "factor": Decimal("0.9")},
            {"trade_date": date(2024, 1, 3), "factor": Decimal("1")},
        ]

    def get_data_version(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return "data-version"

    def fetch_rule_context(self, instrument, start_date, end_date):
        del instrument, start_date, end_date
        return {
            "classifications": [
                {
                    "classification": "main_board",
                    "effective_start": date(2020, 1, 1),
                    "effective_end": None,
                    "confirmed": True,
                },
                {
                    "classification": "non_st",
                    "effective_start": date(2020, 1, 1),
                    "effective_end": None,
                    "confirmed": True,
                },
            ],
            "statuses": [
                {
                    "trade_date": date(2024, 1, 3),
                    "status": "suspended",
                    "confirmed": True,
                }
            ],
        }


def test_raw_and_adjusted_queries_keep_identical_local_session_dates() -> None:
    service = CNMarketHistoryQueryService(_QueryRepository())

    raw = service.load(
        "CNStock:600519.SH",
        date(2024, 1, 2),
        date(2024, 1, 3),
        mode=AdjustmentMode.RAW,
    )
    adjusted = service.load(
        "CNStock:600519.SH",
        date(2024, 1, 2),
        date(2024, 1, 3),
        mode=AdjustmentMode.FORWARD,
    )

    assert raw.frame.index.equals(adjusted.frame.index)
    assert raw.frame.index.tz is None
    assert adjusted.frame.iloc[0]["close"] == 9.0
    assert adjusted.provenance["factorVersion"] == "factor-version"
    assert raw.frame.iloc[1]["previous_close"] == 10.0
    assert raw.frame.iloc[0]["board_classification"] == "main_board"
    assert raw.frame.iloc[0]["status_classification"] == "non_st"
    assert bool(raw.frame.iloc[0]["classification_confirmed"]) is True
    assert bool(raw.frame.iloc[1]["is_suspended"]) is True
