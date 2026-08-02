"""Official corporate-action reference provider backed by a pinned Router stream."""

from __future__ import annotations

from app.services.data_routing.gateway import get_routed_external_data_gateway


class RoutedOfficialAdjustmentReferenceProvider:
    def __init__(self, settings=None, *, gateway=None) -> None:
        self.settings = settings
        self.gateway = gateway
        self._stream = None

    def start_stream(self, run_id: str) -> None:
        if self.gateway is None:
            self.gateway = get_routed_external_data_gateway()
        self._stream = self.gateway.pin_background_stream(
            "task.cn_history.official_adjustments",
            stream_id=f"{run_id}:official-adjustments",
            checkpoint={"run_id": run_id, "instrument": ""},
        )

    def fetch_references(self, instrument, actions):
        if self._stream is None:
            raise RuntimeError("Official adjustment stream must be pinned before execution")
        event_dates = sorted({item.event_date.isoformat() for item in actions})
        return dict(self.gateway.execute_background_chunk(
            "task.cn_history.official_adjustments",
            self._stream,
            {"instrument": instrument.canonical, "event_dates": event_dates},
            constraints={"market": "CN"},
        ).data or {})
