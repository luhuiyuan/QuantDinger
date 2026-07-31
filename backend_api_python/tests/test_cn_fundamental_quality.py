from app.data_sources.cninfo_fundamental_history import map_cninfo_verification
from app.services.cn_fundamental_history.metrics import MetricResult
from app.services.cn_fundamental_history.quality import (
    final_quality_status,
    reconcile_values,
    screen_metrics,
)


def metric(code, value, status="available", **evidence):
    return MetricResult(code, status, value, evidence)


def test_negative_fcf_is_rule_exclusion_not_insufficient():
    metrics = {
        "roe_10y_average": metric("roe_10y_average", 0.2),
        "fcf_5y_cumulative": metric("fcf_5y_cumulative", -1),
        "interest_coverage": metric("interest_coverage", 5),
        "gross_margin_5y_average": metric("gross_margin_5y_average", 0.3),
        "ocf_to_parent_net_income_5y_average": metric("ocf_to_parent_net_income_5y_average", 1),
        "net_margin_5y_average": metric("net_margin_5y_average", 0.2),
        "share_count_growth_5y": metric("share_count_growth_5y", 0.1),
    }
    result = screen_metrics(metrics)
    assert result.status == "excluded"
    assert next(x for x in result.outcomes if x.metric_code == "fcf_5y_cumulative").status == "excluded"


def test_threshold_band_creates_verification_target_for_near_failure():
    result = screen_metrics({"roe": metric("roe", 0.14)}, rules={"roe": (0.15, 1)})
    assert result.status == "excluded"
    assert result.verification_targets[0].metric_code == "roe"


def test_reconciliation_warning_and_blocking_thresholds():
    assert reconcile_values(100, 102).status == "warning"
    blocked = reconcile_values(100, 106)
    assert blocked.status == "blocking"
    assert blocked.difference_pct > 0.05


def test_cninfo_missing_structured_value_is_explicitly_insufficient():
    result = map_cninfo_verification({"announcementId": "x"})
    assert result == {
        "status": "insufficient",
        "fields": {},
        "mapping_version": "cninfo-structured-verification-v1",
        "reason": "structured_fields_unavailable",
    }


def test_unavailable_official_value_cannot_be_formally_verified():
    screening = screen_metrics({"fcf": metric("fcf", 10)}, rules={"fcf": (0, 1)})
    assert final_quality_status(screening, {"fcf": reconcile_values(10, None)}) == "pending_verification"
