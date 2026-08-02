"""Official macro time-series providers for research agents."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional


class MacroSeriesProvider:
    """Small typed client for stable US macro sources."""

    def source_status(self) -> List[Dict[str, Any]]:
        return [
            {
                "provider": "FRED",
                "configured": None,
                "available": None,
                "purpose": "US macro time series: rates, inflation, labor, financial conditions.",
                "note": "Managed by Data Source Operations routing policy.",
            },
            {
                "provider": "BLS",
                "configured": None,
                "available": None,
                "purpose": "Official CPI, employment, wages, and labor market series.",
                "note": "Managed by Data Source Operations routing policy.",
            },
            {
                "provider": "BEA",
                "configured": None,
                "available": None,
                "purpose": "Official GDP, income, consumption, and national accounts data.",
                "note": "Managed by Data Source Operations routing policy.",
            },
        ]

    def fetch_fred_series(
        self,
        series_id: str,
        start: Optional[date | str] = None,
        end: Optional[date | str] = None,
        limit: int = 120,
    ) -> Dict[str, Any]:
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        return dict(get_routed_external_data_gateway().execute(
            "market.macro.series",
            {"operation": "fred_series", "series_id": series_id, "start": str(start) if start else None, "end": str(end) if end else None, "limit": limit},
            constraints={"venue": "fred"},
        ).data or {})

    def fetch_bls_series(
        self,
        series_ids: Iterable[str],
        start_year: int,
        end_year: int,
    ) -> Dict[str, Any]:
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        return dict(get_routed_external_data_gateway().execute(
            "market.macro.series",
            {"operation": "bls_series", "series_ids": list(series_ids), "start_year": start_year, "end_year": end_year},
            constraints={"venue": "bls"},
        ).data or {})

    def fetch_bea_data(self, dataset: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from app.services.data_routing.gateway import get_routed_external_data_gateway

        return dict(get_routed_external_data_gateway().execute(
            "market.macro.series",
            {"operation": "bea_dataset", "dataset": dataset, "params": dict(params or {})},
            constraints={"venue": "bea"},
        ).data or {})


_macro_series_provider: Optional[MacroSeriesProvider] = None


def get_macro_series_provider() -> MacroSeriesProvider:
    global _macro_series_provider
    if _macro_series_provider is None:
        _macro_series_provider = MacroSeriesProvider()
    return _macro_series_provider
