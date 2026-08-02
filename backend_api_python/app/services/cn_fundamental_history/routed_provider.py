"""Pinned unified-routing fetchers for durable CN fundamental ingestion."""

from __future__ import annotations

from app.services.data_routing.gateway import get_routed_external_data_gateway


class RoutedCNFundamentalFetcher:
    def __init__(self, *, gateway=None) -> None:
        self.gateway = gateway
        self.stream = None

    def start_stream(self, run_id: str) -> None:
        if self.gateway is None:
            self.gateway = get_routed_external_data_gateway()
        self.stream = self.gateway.pin_background_stream(
            "task.cn_fundamental_history.eastmoney",
            stream_id=f"{run_id}:fundamentals",
            checkpoint={"run_id": run_id, "instrument": ""},
        )

    def __call__(self, instrument: str):
        if self.stream is None:
            raise RuntimeError("CN fundamental stream must be pinned before execution")
        return list(self.gateway.execute_background_chunk(
            "task.cn_fundamental_history.eastmoney",
            self.stream,
            {"instrument": instrument},
            constraints={"market": "CN"},
        ).data or [])
