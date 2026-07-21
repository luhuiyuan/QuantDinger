from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from app.services.cn_market_history.instruments import parse_cn_instrument
from app.services.cn_market_history.models import AdjustmentMode, RawDailyBar
from app.services.cn_market_history.query_service import CNMarketHistoryQueryService
from app.services.cn_market_history.repository import _json


def test_json_payload_replaces_non_finite_provider_values_with_null() -> None:
    payload = json.loads(
        _json(
            {
                "float_nan": float("nan"),
                "float_inf": float("inf"),
                "decimal_nan": Decimal("NaN"),
                "nested": [1, float("-inf")],
            }
        )
    )

    assert payload == {
        "decimal_nan": None,
        "float_inf": None,
        "float_nan": None,
        "nested": [1, None],
    }


def test_query_uses_preceding_bar_for_first_requested_previous_close() -> None:
    instrument = parse_cn_instrument("600519")

    def bar(day: int, close: str) -> RawDailyBar:
        value = Decimal(close)
        return RawDailyBar(
            instrument=instrument,
            trade_date=date(2024, 1, day),
            open=value,
            high=value,
            low=value,
            close=value,
            volume=Decimal("1"),
            amount=Decimal("1"),
            provider="easy_tdx",
            provider_version="1.20.4",
            content_hash=f"bar-{day}",
            collected_at=datetime.now(timezone.utc),
        )

    class Repository:
        def fetch_daily_bars(self, *_args, **_kwargs):
            return [bar(2, "100"), bar(3, "101")]

        def get_data_version(self, *_args, **_kwargs):
            return "bars-v1"

        def fetch_rule_context(self, *_args, **_kwargs):
            return {"classifications": [], "statuses": []}

    result = CNMarketHistoryQueryService(Repository()).load(
        instrument.canonical,
        date(2024, 1, 3),
        date(2024, 1, 3),
        mode=AdjustmentMode.RAW,
    )

    assert list(result.frame.index.date) == [date(2024, 1, 3)]
    assert result.frame.iloc[0]["previous_close"] == 100.0
