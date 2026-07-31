from app.services.cn_fundamental_history.industry import (
    is_interest_coverage_exempt,
    map_eastmoney_industry,
)


def test_only_mapped_csrc_bank_and_insurance_are_interest_exempt():
    bank = map_eastmoney_industry("银行")
    assert bank["industryCode"] == "J66"
    assert is_interest_coverage_exempt(bank) is True
    assert is_interest_coverage_exempt(map_eastmoney_industry("证券")) is False
    assert is_interest_coverage_exempt({"taxonomy": "eastmoney", "industryCode": "J66"}) is False
