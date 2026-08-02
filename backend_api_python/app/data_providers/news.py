"""Financial news and economic calendar data providers."""
from __future__ import annotations

from typing import Any, Dict, List

from app.data_providers.economic_calendar import (
    get_economic_calendar,
    get_economic_calendar_payload,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


def fetch_financial_news(lang: str = "all") -> Dict[str, List[Dict[str, Any]]]:
    """Fetch financial news using search service — separated by language."""
    from app.services.data_routing.gateway import get_routed_external_data_gateway

    return dict(get_routed_external_data_gateway().execute(
        "analysis.news_search",
        {"operation": "financial_news", "language": lang},
        constraints={"market": "GLOBAL"},
    ).data or {"cn": [], "en": []})


__all__ = ["fetch_financial_news", "get_economic_calendar", "get_economic_calendar_payload"]
