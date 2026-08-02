from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.cn_market_history.instruments import parse_cn_instrument
from app.services.cn_market_history.routed_tdx_provider import RoutedTDXProvider
from app.services.cn_market_history.tdx_provider import DailyBarPage


class Gateway:
    def __init__(self):
        self.pins = []
        self.calls = []

    def pin_background_stream(self, feature, **kwargs):
        stream = SimpleNamespace(capability_key=feature, instance_id=len(self.pins) + 1)
        self.pins.append((feature, kwargs, stream))
        return stream

    def execute_background_chunk(self, feature, stream, subject, **kwargs):
        self.calls.append((feature, stream, subject, kwargs))
        if "event_dates" in subject:
            return SimpleNamespace(data={})
        if subject["operation"] == "metadata":
            return SimpleNamespace(data=("metadata", ()))
        if subject["operation"] == "daily_page":
            return SimpleNamespace(data=DailyBarPage(subject["start_offset"], subject["start_offset"], 0, (), True))
        return SimpleNamespace(data=[])


def test_cn_history_provider_pins_two_capability_streams_and_never_selects_locally():
    gateway = Gateway()
    provider = RoutedTDXProvider(gateway=gateway)
    provider.start_streams("run-1")
    instrument = parse_cn_instrument("600000.SH")

    with provider:
        assert provider.fetch_instrument_metadata(instrument)[0] == "metadata"
        assert list(provider.iter_daily_pages(instrument, date(2026, 1, 1), date(2026, 1, 31)))[0].reached_start
        assert provider.fetch_corporate_actions(instrument) == []

    assert [item[0] for item in gateway.pins] == [
        "task.cn_history.daily_bars",
        "task.cn_history.provider_actions",
    ]
    assert [item[0] for item in gateway.calls] == [
        "task.cn_history.daily_bars",
        "task.cn_history.daily_bars",
        "task.cn_history.provider_actions",
    ]


def test_official_reference_provider_uses_one_pinned_stream():
    from app.services.cn_market_history.routed_official_adjustments import (
        RoutedOfficialAdjustmentReferenceProvider,
    )

    gateway = Gateway()
    provider = RoutedOfficialAdjustmentReferenceProvider(gateway=gateway)
    provider.start_stream("run-2")
    instrument = parse_cn_instrument("600000.SH")
    action = SimpleNamespace(event_date=date(2026, 1, 5))

    assert provider.fetch_references(instrument, [action]) == {}
    assert gateway.pins[0][0] == "task.cn_history.official_adjustments"
    assert gateway.calls[0][2]["event_dates"] == ["2026-01-05"]
