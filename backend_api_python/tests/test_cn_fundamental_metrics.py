import pytest
from app.services.cn_fundamental_history.metrics import FundamentalMetricCalculator

def row(year, **values):
    return {"period_end": f"{year}-12-31", "parent_net_income": 10, "parent_equity_begin": 100, "parent_equity_end": 100, "operating_cash_flow": 12, "capex_cash_paid": 3, "ebit": 20, "interest_expense": 4, "revenue": 100, "gross_profit": 25, "total_shares": 100, **values}

def test_calculates_quality_metrics_from_raw_annual_fields():
    result = FundamentalMetricCalculator().calculate([row(year) for year in range(2016, 2026)])
    assert result["roe_10y_average"].value == pytest.approx(0.1)
    assert result["fcf_5y_cumulative"].value == 45
    assert result["interest_coverage"].value == 5
    assert result["gross_margin_5y_average"].value == pytest.approx(0.25)
    assert result["net_margin_5y_average"].value == pytest.approx(0.1)

def test_missing_interest_expense_is_insufficient_not_finance_expense_proxy():
    result = FundamentalMetricCalculator().calculate([row(year) for year in range(2016, 2026)[:-1]] + [row(2025, interest_expense=None, finance_expense=1)])
    assert result["interest_coverage"].status == "insufficient"


def test_share_growth_uses_year_end_values_five_years_apart():
    rows = [row(year, total_shares=100) for year in range(2020, 2025)] + [row(2025, total_shares=130)]
    result = FundamentalMetricCalculator().calculate(rows)
    assert result["share_count_growth_5y"].value == pytest.approx(0.30)
