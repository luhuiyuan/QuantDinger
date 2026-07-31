from datetime import date

from app.services.cn_fundamental_history.service import CNFundamentalVerificationService


class Repository:
    def __init__(self):
        self.rows = []

    def record_reconciliation(self, target_id, field_code, result, **context):
        self.rows.append((target_id, field_code, result, context))

    def finalize_verification_target(self, target_id):
        return "blocked" if any(row[2].status == "blocking" for row in self.rows) else "verified"


def test_source_difference_over_five_percent_blocks_target_with_evidence():
    repository = Repository()
    result = CNFundamentalVerificationService(repository).reconcile_target(
        target_id=9,
        instrument="CNStock:600519.SH",
        period_end=date(2025, 12, 31),
        primary_fields={"revenue": 100},
        official_fields={"revenue": 106},
    )
    assert result["status"] == "blocked"
    assert repository.rows[0][2].difference_pct > 0.05
    assert repository.rows[0][3]["instrument"] == "CNStock:600519.SH"
