from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.cn_market_history.adjustments import calculate_adjustment_factors
from app.services.cn_market_history.config import CNMarketHistorySettings
from app.services.cn_market_history.instruments import parse_cn_instrument
from app.services.cn_market_history.models import AdjustmentMode, CorporateAction, RawDailyBar
from app.services.cn_market_history.official_adjustments import (
    OfficialAdjustmentMismatch,
    OfficialAdjustmentReference,
    OfficialAdjustmentReferenceProvider,
    relevant_corporate_actions,
    verify_corporate_action_references,
)


def _settings() -> CNMarketHistorySettings:
    return CNMarketHistorySettings(
        enabled=False,
        sync_enabled=False,
        page_size=800,
        write_batch_size=200,
        request_interval_seconds=0.0,
        provider_timeout_seconds=5.0,
        provider_retry_attempts=0,
        max_targets_per_run=10,
        incremental_lookback_days=14,
        daily_symbols=(),
        disk_path="/",
        disk_soft_free_bytes=5_000,
        disk_hard_free_bytes=2_000,
    )


def _action(code: str, event_date: date, cash: str, *, bonus: str = "0") -> CorporateAction:
    instrument = parse_cn_instrument(code)
    return CorporateAction(
        instrument=instrument,
        event_date=event_date,
        category=1,
        event_name="除权除息",
        cash_dividend=Decimal(cash),
        bonus_ratio=Decimal(bonus),
        rights_ratio=Decimal("0"),
        rights_price=Decimal("0"),
        provider="easy_tdx",
        provider_version="1.20.4",
        content_hash=f"tdx-{code}-{event_date}",
    )


def _bar(code: str, trade_date: date, close: str) -> RawDailyBar:
    instrument = parse_cn_instrument(code)
    value = Decimal(close)
    return RawDailyBar(
        instrument=instrument,
        trade_date=trade_date,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1000"),
        amount=Decimal("10000"),
        provider="easy_tdx",
        provider_version="1.20.4",
        content_hash=f"bar-{code}-{trade_date}",
        collected_at=datetime.now(timezone.utc),
    )


def _reference(action: CorporateAction, price: str, *, source: str) -> OfficialAdjustmentReference:
    return OfficialAdjustmentReference(
        instrument=action.instrument.canonical,
        event_date=action.event_date,
        reference_price=Decimal(price),
        source=source,
        source_url="https://official.example/reference",
        response_hash="a" * 64,
        retrieved_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        evidence={},
    )


def test_sse_reference_provider_parses_structured_pre_close_price() -> None:
    action = _action("600519", date(2025, 6, 26), "27.673")

    def requester(url, params, headers, timeout):
        assert params["COMPANY_CODE"] == "600519"
        assert "sse.com.cn" in headers["Referer"]
        assert timeout == 5.0
        return (
            {
                "result": [
                    {
                        "A_STOCK_CODE": "600519",
                        "A_DIV_DATE": "20250626",
                        "A_REG_DATE": "20250625",
                        "A_BEFR_TAX_DIV": "27.673",
                        "A_STOCK_VOL": "125288.77",
                        "PRE_CLOSE_PRICE": "1408.26",
                    }
                ]
            },
            b"sse-response",
            url + "?resolved=1",
        )

    provider = OfficialAdjustmentReferenceProvider(_settings(), requester=requester)
    result = provider.fetch_references(action.instrument, [action])

    reference = result[action.event_date]
    assert reference.reference_price == Decimal("1408.26")
    assert reference.source == "sse_dividend"
    assert len(reference.response_hash) == 64


def test_szse_reference_provider_parses_daily_quote_reference() -> None:
    action = _action("000001", date(2025, 6, 12), "0.362")

    def requester(url, params, headers, timeout):
        del headers, timeout
        assert params["txtBeginDate"] == "2025-06-12"
        return (
            [
                {
                    "data": [
                        {
                            "jyrq": "2025-06-12",
                            "zqdm": "000001",
                            "zqjc": "平安银行",
                            "qss": "11.49",
                        }
                    ]
                }
            ],
            b"szse-response",
            url + "?resolved=1",
        )

    provider = OfficialAdjustmentReferenceProvider(_settings(), requester=requester)
    result = provider.fetch_references(action.instrument, [action])

    reference = result[action.event_date]
    assert reference.reference_price == Decimal("11.49")
    assert reference.source == "szse_daily_quote"


def test_szse_reference_provider_falls_back_to_cninfo_parameters() -> None:
    action = _action("000001", date(2026, 6, 12), "0.36")

    def requester(url, params, headers, timeout):
        del timeout
        if "szse.cn" in url:
            return ([{"data": []}], b"szse-empty", url)
        assert params == {"scode": "000001"}
        assert headers["Accept-Enckey"]
        return (
            {
                "records": [
                    {
                        "F020D": "2026-06-12",
                        "F012N": 3.6,
                        "F010N": None,
                        "F011N": None,
                        "F006D": "2026-06-05",
                        "F018D": "2026-06-11",
                        "F007V": "10派3.6元(含税)",
                        "F001V": "2025年报",
                    }
                ]
            },
            b"cninfo-response",
            url + "?scode=000001",
        )

    provider = OfficialAdjustmentReferenceProvider(_settings(), requester=requester)
    reference = provider.fetch_references(action.instrument, [action])[action.event_date]

    assert reference.reference_price is None
    assert reference.source == "cninfo_dividend_parameters"
    assert reference.evidence["cash_dividend_per_share"] == "0.36"


def test_normal_cash_dividend_keeps_precise_tdx_formula_after_official_check() -> None:
    action = _action("000001", date(2025, 6, 12), "0.362")
    bars = (
        _bar("000001", date(2025, 6, 11), "11.85"),
        _bar("000001", date(2025, 6, 12), "11.68"),
    )

    verified = verify_corporate_action_references(
        bars,
        [action],
        {action.event_date: _reference(action, "11.49", source="szse_daily_quote")},
    )[0]

    audit = verified.raw_payload["official_adjustment"]
    assert audit["status"] == "verified_formula"
    assert Decimal(audit["adjustment_ratio"]) == (
        Decimal("11.85") - Decimal("0.362")
    ) / Decimal("11.85")


def test_official_cash_parameter_generates_audited_ratio_when_quote_archive_lags() -> None:
    action = _action("000001", date(2026, 6, 12), "0.3599999904632568")
    bars = (
        _bar("000001", date(2026, 6, 11), "11.30"),
        _bar("000001", date(2026, 6, 12), "11.24"),
    )
    reference = OfficialAdjustmentReference(
        instrument=action.instrument.canonical,
        event_date=action.event_date,
        reference_price=None,
        source="cninfo_dividend_parameters",
        source_url="https://webapi.cninfo.com.cn/official",
        response_hash="c" * 64,
        retrieved_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        evidence={"cash_dividend_per_share": "0.36"},
    )

    verified = verify_corporate_action_references(
        bars, [action], {action.event_date: reference}
    )[0]

    audit = verified.raw_payload["official_adjustment"]
    assert audit["status"] == "verified_official_parameters"
    assert audit["reference_price"] == "10.94"
    assert Decimal(audit["adjustment_ratio"]) == Decimal("10.94") / Decimal("11.30")


def test_differential_cash_dividend_uses_official_reference_ratio() -> None:
    action = _action("600519", date(2025, 6, 26), "27.673001098632813")
    bars = (
        _bar("600519", date(2025, 6, 25), "1435.86"),
        _bar("600519", date(2025, 6, 26), "1420.00"),
    )

    verified = verify_corporate_action_references(
        bars,
        [action],
        {action.event_date: _reference(action, "1408.26", source="sse_dividend")},
    )[0]
    factors = calculate_adjustment_factors(
        bars,
        [verified],
        AdjustmentMode.FORWARD,
    )

    audit = verified.raw_payload["official_adjustment"]
    expected = Decimal("1408.26") / Decimal("1435.86")
    assert audit["status"] == "official_reference_override"
    assert Decimal(audit["adjustment_ratio"]) == expected
    assert factors[0].factor == expected
    assert factors[1].factor == Decimal("1")


def test_complex_mismatch_fails_closed() -> None:
    action = _action("600519", date(2025, 6, 26), "1.0", bonus="0.1")
    bars = (
        _bar("600519", date(2025, 6, 25), "100"),
        _bar("600519", date(2025, 6, 26), "95"),
    )

    with pytest.raises(OfficialAdjustmentMismatch):
        verify_corporate_action_references(
            bars,
            [action],
            {action.event_date: _reference(action, "80", source="sse_dividend")},
        )


def test_only_actions_affecting_the_loaded_bar_range_require_verification() -> None:
    current = _action("000001", date(2025, 6, 12), "0.362")
    historical = _action("000001", date(2024, 6, 14), "0.719")
    bars = (
        _bar("000001", date(2025, 6, 11), "11.85"),
        _bar("000001", date(2025, 6, 12), "11.68"),
    )

    assert relevant_corporate_actions(bars, [historical, current]) == (current,)


def test_retrieval_time_and_full_response_hash_do_not_change_factor_content_identity() -> None:
    action = _action("000001", date(2025, 6, 12), "0.362")
    bars = (
        _bar("000001", date(2025, 6, 11), "11.85"),
        _bar("000001", date(2025, 6, 12), "11.68"),
    )
    first = _reference(action, "11.49", source="szse_daily_quote")
    second = OfficialAdjustmentReference(
        instrument=first.instrument,
        event_date=first.event_date,
        reference_price=first.reference_price,
        source=first.source,
        source_url=first.source_url,
        response_hash="b" * 64,
        retrieved_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        evidence=first.evidence,
    )

    first_action = verify_corporate_action_references(
        bars, [action], {action.event_date: first}
    )[0]
    second_action = verify_corporate_action_references(
        bars, [action], {action.event_date: second}
    )[0]

    assert first_action.content_hash == second_action.content_hash
    assert (
        first_action.raw_payload["official_adjustment"]["response_hash"]
        != second_action.raw_payload["official_adjustment"]["response_hash"]
    )
