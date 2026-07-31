"""Pure screening and source-reconciliation rules for annual fundamentals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .metrics import MetricResult

WARNING_DIFFERENCE = 0.01
BLOCKING_DIFFERENCE = 0.05
DEFAULT_NEAR_THRESHOLD_BAND = 0.10

# A positive direction means the value must be >= threshold; a negative
# direction means it must be <= threshold. FCF is intentionally zero based.
DEFAULT_RULES: dict[str, tuple[float, int]] = {
    "roe_10y_average": (0.15, 1),
    "fcf_5y_cumulative": (0.0, 1),
    "interest_coverage": (3.0, 1),
    "gross_margin_5y_average": (0.20, 1),
    "ocf_to_parent_net_income_5y_average": (0.80, 1),
    "net_margin_5y_average": (0.10, 1),
    "share_count_growth_5y": (0.20, -1),
}


@dataclass(frozen=True)
class RuleOutcome:
    metric_code: str
    status: str  # passed / excluded / insufficient
    value: float | None
    threshold: float
    needs_verification: bool
    evidence: dict


@dataclass(frozen=True)
class ScreeningResult:
    status: str  # passed_pending_verification / excluded / insufficient
    outcomes: tuple[RuleOutcome, ...]
    verification_targets: tuple[RuleOutcome, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    status: str  # matched / warning / blocking / unavailable
    primary_value: float | None
    official_value: float | None
    difference_pct: float | None
    evidence: dict


def screen_metrics(
    metrics: Mapping[str, MetricResult],
    *,
    rules: Mapping[str, tuple[float, int]] = DEFAULT_RULES,
    near_band: float = DEFAULT_NEAR_THRESHOLD_BAND,
) -> ScreeningResult:
    outcomes: list[RuleOutcome] = []
    for code, (threshold, direction) in rules.items():
        metric = metrics.get(code)
        if metric is None or metric.status != "available":
            outcomes.append(RuleOutcome(code, "insufficient", None, threshold, False, {"metricStatus": getattr(metric, "status", "missing")}))
            continue
        if metric.evidence.get("exempt"):
            outcomes.append(RuleOutcome(code, "passed", None, threshold, False, {"exempt": True}))
            continue
        value = metric.value
        if value is None:
            outcomes.append(RuleOutcome(code, "insufficient", None, threshold, False, {"reason": "missing_value"}))
            continue
        passed = value >= threshold if direction > 0 else value <= threshold
        # A zero threshold cannot use a relative band. Verify values within a
        # small absolute epsilon around zero and every passing instrument.
        near = abs(value - threshold) <= (abs(threshold) * near_band if threshold else near_band)
        outcomes.append(RuleOutcome(code, "passed" if passed else "excluded", value, threshold, passed or near, {"direction": direction, "nearBand": near_band}))

    if any(item.status == "insufficient" for item in outcomes):
        status = "insufficient"
    elif any(item.status == "excluded" for item in outcomes):
        status = "excluded"
    else:
        status = "passed_pending_verification"
    return ScreeningResult(status, tuple(outcomes), tuple(item for item in outcomes if item.needs_verification))


def reconcile_values(primary_value, official_value, *, evidence: Mapping | None = None) -> ReconciliationResult:
    try:
        primary = float(primary_value)
        official = float(official_value)
    except (TypeError, ValueError):
        return ReconciliationResult("unavailable", None, None, None, {**(evidence or {}), "reason": "value_unavailable"})
    denominator = max(abs(primary), abs(official))
    difference = 0.0 if denominator == 0 else abs(primary - official) / denominator
    status = "blocking" if difference > BLOCKING_DIFFERENCE else "warning" if difference > WARNING_DIFFERENCE else "matched"
    return ReconciliationResult(status, primary, official, difference, dict(evidence or {}))


def final_quality_status(screening: ScreeningResult, reconciliations: Mapping[str, ReconciliationResult]) -> str:
    if screening.status == "excluded":
        return "excluded"
    if screening.status == "insufficient":
        return "insufficient"
    if not reconciliations or any(item.status == "unavailable" for item in reconciliations.values()):
        return "pending_verification"
    if any(item.status == "blocking" for item in reconciliations.values()):
        return "blocked"
    return "verified_with_warnings" if any(item.status == "warning" for item in reconciliations.values()) else "verified"
