"""Deterministic annual quality metrics from retained raw statement fields."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

FORMULA_VERSION = "cn-annual-quality-v1"
WINDOW = 5

@dataclass(frozen=True)
class MetricResult:
    code: str
    status: str  # available / insufficient / invalid
    value: float | None
    evidence: dict

class FundamentalMetricCalculator:
    """Pure calculator: callers provide only PIT-eligible annual observations."""
    def calculate(self, observations: Iterable[Mapping]) -> dict[str, MetricResult]:
        rows = sorted(observations, key=lambda row: str(row.get("period_end") or ""))
        annual = rows[-WINDOW:]
        return {
            "roe_10y_average": self._average_roe(rows[-10:]),
            "fcf_5y_cumulative": self._fcf(annual),
            "interest_coverage": self._interest(rows[-1:] if rows else []),
            "gross_margin_5y_average": self._margin(annual, "gross_profit", "revenue", "gross_margin_5y_average"),
            "ocf_to_parent_net_income_5y_average": self._ocf_conversion(annual),
            "net_margin_5y_average": self._margin(annual, "parent_net_income", "revenue", "net_margin_5y_average"),
            "share_count_growth_5y": self._share_growth(rows),
        }

    @staticmethod
    def _number(row: Mapping, name: str) -> float | None:
        try:
            value = float(row.get(name))
            return value if value == value and abs(value) != float("inf") else None
        except (TypeError, ValueError):
            return None

    def _average_roe(self, rows: list[Mapping]) -> MetricResult:
        values = []
        for row in rows:
            income, start, end = (self._number(row, key) for key in ("parent_net_income", "parent_equity_begin", "parent_equity_end"))
            if income is None or start is None or end is None or start + end == 0:
                return MetricResult("roe_10y_average", "insufficient", None, {"requiredYears": 10})
            values.append(income / ((start + end) / 2.0))
        return MetricResult("roe_10y_average", "available", sum(values) / len(values), {"years": len(values), "formulaVersion": FORMULA_VERSION}) if len(values) == 10 else MetricResult("roe_10y_average", "insufficient", None, {"requiredYears": 10, "years": len(values)})

    def _fcf(self, rows: list[Mapping]) -> MetricResult:
        values = []
        for row in rows:
            ocf, capex = self._number(row, "operating_cash_flow"), self._number(row, "capex_cash_paid")
            if ocf is None or capex is None:
                return MetricResult("fcf_5y_cumulative", "insufficient", None, {"requiredYears": WINDOW})
            # The mapped field represents cash paid for long-lived assets and
            # is stored as a positive cash outflow by the adapters.
            values.append(ocf - capex)
        return MetricResult("fcf_5y_cumulative", "available", sum(values), {"years": len(values), "formulaVersion": FORMULA_VERSION}) if len(values) == WINDOW else MetricResult("fcf_5y_cumulative", "insufficient", None, {"requiredYears": WINDOW, "years": len(values)})

    def _interest(self, rows: list[Mapping]) -> MetricResult:
        if not rows: return MetricResult("interest_coverage", "insufficient", None, {})
        row = rows[-1]
        if row.get("industry_exempt_interest_coverage"):
            return MetricResult("interest_coverage", "available", None, {"exempt": True, "formulaVersion": FORMULA_VERSION})
        ebit, interest = self._number(row, "ebit"), self._number(row, "interest_expense")
        if ebit is None or interest is None: return MetricResult("interest_coverage", "insufficient", None, {"required": ["ebit", "interest_expense"]})
        if interest <= 0: return MetricResult("interest_coverage", "invalid", None, {"reason": "non_positive_interest_expense"})
        return MetricResult("interest_coverage", "available", ebit / interest, {"formulaVersion": FORMULA_VERSION})

    def _margin(self, rows: list[Mapping], numerator: str, denominator: str, code: str) -> MetricResult:
        values = []
        for row in rows:
            a, b = self._number(row, numerator), self._number(row, denominator)
            if a is None or b is None or b == 0: return MetricResult(code, "insufficient", None, {"requiredYears": WINDOW})
            values.append(a / b)
        return MetricResult(code, "available", sum(values) / len(values), {"years": len(values), "formulaVersion": FORMULA_VERSION}) if len(values) == WINDOW else MetricResult(code, "insufficient", None, {"requiredYears": WINDOW, "years": len(values)})

    def _ocf_conversion(self, rows: list[Mapping]) -> MetricResult:
        values = []
        for row in rows:
            ocf, income = self._number(row, "operating_cash_flow"), self._number(row, "parent_net_income")
            if ocf is None or income is None or income <= 0: return MetricResult("ocf_to_parent_net_income_5y_average", "insufficient", None, {"requiredYears": WINDOW})
            values.append(ocf / income)
        return MetricResult("ocf_to_parent_net_income_5y_average", "available", sum(values) / len(values), {"years": len(values), "formulaVersion": FORMULA_VERSION}) if len(values) == WINDOW else MetricResult("ocf_to_parent_net_income_5y_average", "insufficient", None, {"requiredYears": WINDOW, "years": len(values)})

    def _share_growth(self, rows: list[Mapping]) -> MetricResult:
        if len(rows) < 6: return MetricResult("share_count_growth_5y", "insufficient", None, {"requiredYears": 5})
        first, last = self._number(rows[-6], "total_shares"), self._number(rows[-1], "total_shares")
        if first is None or last is None or first <= 0: return MetricResult("share_count_growth_5y", "insufficient", None, {"required": "total_shares"})
        return MetricResult("share_count_growth_5y", "available", last / first - 1.0, {"formulaVersion": FORMULA_VERSION})
