"""Durable China A-share daily history services."""

from .config import CNMarketHistorySettings, load_cn_market_history_settings
from .instruments import CNInstrumentError, parse_cn_instrument
from .models import (
    AdjustmentFactor,
    AdjustmentMode,
    CNClassification,
    CNInstrument,
    CNInstrumentMetadata,
    CorporateAction,
    CoverageGap,
    CoverageReport,
    DataProvenance,
    ProviderProbe,
    QualityFinding,
    QualitySeverity,
    RawDailyBar,
    SyncStatus,
    SyncTarget,
)
from .operations_repository import CNMarketHistoryOperationsRepository
from .repository import CNMarketHistoryRepository, UpsertSummary
from .tdx_provider import DailyBarPage, TDXProvider, TDXProviderError
from .sync_service import (
    CNHistoryDiskBlocked,
    CNHistoryDuplicateRun,
    CNHistorySyncDisabled,
    CNHistorySyncError,
    CNMarketHistorySyncService,
)
from .adjustments import ALGORITHM_VERSION, calculate_adjustment_factors
from .quality import CNMarketHistoryQualityService
from .query_service import CNMarketHistoryQueryService, HistoryQueryResult
from .official_adjustments import (
    OfficialAdjustmentError,
    OfficialAdjustmentMismatch,
    OfficialAdjustmentReference,
    OfficialAdjustmentReferenceProvider,
    OfficialAdjustmentSourceError,
)

__all__ = [
    "AdjustmentFactor",
    "ALGORITHM_VERSION",
    "AdjustmentMode",
    "CNClassification",
    "CNInstrument",
    "CNInstrumentMetadata",
    "CNInstrumentError",
    "CNMarketHistorySettings",
    "CNMarketHistoryOperationsRepository",
    "CNMarketHistoryRepository",
    "CNMarketHistoryQualityService",
    "CNMarketHistoryQueryService",
    "CNMarketHistorySyncService",
    "CNHistoryDiskBlocked",
    "CNHistoryDuplicateRun",
    "CNHistorySyncDisabled",
    "CNHistorySyncError",
    "CorporateAction",
    "CoverageGap",
    "CoverageReport",
    "DataProvenance",
    "DailyBarPage",
    "ProviderProbe",
    "QualityFinding",
    "QualitySeverity",
    "RawDailyBar",
    "SyncStatus",
    "SyncTarget",
    "TDXProvider",
    "TDXProviderError",
    "UpsertSummary",
    "HistoryQueryResult",
    "OfficialAdjustmentError",
    "OfficialAdjustmentMismatch",
    "OfficialAdjustmentReference",
    "OfficialAdjustmentReferenceProvider",
    "OfficialAdjustmentSourceError",
    "calculate_adjustment_factors",
    "load_cn_market_history_settings",
    "parse_cn_instrument",
]
