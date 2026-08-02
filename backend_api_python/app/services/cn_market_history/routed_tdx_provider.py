"""CN history provider compatibility surface backed by pinned Router streams."""

from __future__ import annotations

from datetime import date
from typing import Iterator

from app.services.data_routing.gateway import get_routed_external_data_gateway

from .models import CNInstrument
from .tdx_provider import DailyBarPage


class RoutedTDXProvider:
    """Preserve the history service contract while removing direct Provider selection."""

    selected_host = ""

    def __init__(self, settings=None, *, gateway=None) -> None:
        self.settings = settings
        self.gateway = gateway
        self._daily_stream = None
        self._actions_stream = None

    @property
    def provider_version(self) -> str:
        return "unified-router"

    def start_streams(self, run_id: str) -> None:
        if self.gateway is None:
            self.gateway = get_routed_external_data_gateway()
        self._daily_stream = self.gateway.pin_background_stream(
            "task.cn_history.daily_bars",
            stream_id=f"{run_id}:daily-bars",
            checkpoint={"run_id": run_id, "page_offset": 0},
        )
        self._actions_stream = self.gateway.pin_background_stream(
            "task.cn_history.provider_actions",
            stream_id=f"{run_id}:corporate-actions",
            checkpoint={"run_id": run_id, "instrument": ""},
        )

    def probe_hosts(self) -> list:
        return []

    def __enter__(self) -> "RoutedTDXProvider":
        if self._daily_stream is None or self._actions_stream is None:
            raise RuntimeError("Routed TDX streams must be pinned before execution")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback

    def _daily(self, subject: dict):
        return self.gateway.execute_background_chunk(
            "task.cn_history.daily_bars",
            self._daily_stream,
            subject,
            constraints={"market": "CN", "adjustment": "raw"},
        ).data

    def fetch_instrument_metadata(self, instrument: CNInstrument):
        return self._daily({"operation": "metadata", "instrument": instrument.canonical})

    def iter_daily_pages(
        self,
        instrument: CNInstrument,
        start_date: date,
        end_date: date,
        *,
        start_offset: int = 0,
    ) -> Iterator[DailyBarPage]:
        offset = max(0, int(start_offset))
        while True:
            page = self._daily({
                "operation": "daily_page",
                "instrument": instrument.canonical,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "start_offset": offset,
            })
            if not isinstance(page, DailyBarPage):
                raise TypeError("easy_tdx Adapter returned an invalid daily page")
            yield page
            if page.reached_start:
                return
            offset = page.next_offset

    def fetch_corporate_actions(self, instrument: CNInstrument):
        return list(self.gateway.execute_background_chunk(
            "task.cn_history.provider_actions",
            self._actions_stream,
            {"operation": "corporate_actions", "instrument": instrument.canonical},
            constraints={"market": "CN"},
        ).data or [])
