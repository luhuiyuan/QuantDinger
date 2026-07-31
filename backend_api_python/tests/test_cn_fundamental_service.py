from datetime import date

from app.services.cn_fundamental_history.service import CNFundamentalHistoryService


class Repository:
    def __init__(self):
        self.appended = []
        self.ensured = []

    def ensure_instrument(self, instrument):
        self.ensured.append(instrument)

    def append_observation(self, **kwargs):
        self.appended.append(kwargs)
        return len(self.appended)

    def load_as_of(self, instrument, as_of): return []
    def refresh_coverage(self, instrument, **kwargs): return {"last_period_end": None}


def test_announcement_later_than_as_of_is_not_written_or_calculated():
    repository = Repository()
    fetcher = lambda code: [{
        "period_end": date(2025, 12, 31), "available_at": date(2026, 4, 30),
        "raw": {"REPORT_DATE": "2025-12-31"}, "fields": {"revenue": 1},
        "source_version": "v2", "request_context": {}, "announcement_ref": {},
        "mapping_version": "test",
    }]
    result = CNFundamentalHistoryService(repository=repository, fetcher=fetcher).sync_instrument(
        "CNStock:600519.SH", as_of=date(2026, 4, 1)
    )
    assert repository.appended == []
    assert result["observationsWritten"] == 0


def test_sync_materializes_catalog_instrument_before_fetching_observations():
    repository = Repository()

    def fetcher(code):
        assert code == "300750"
        assert repository.ensured == ["CNStock:300750.SZ"]
        return []

    CNFundamentalHistoryService(
        repository=repository, fetcher=fetcher
    ).sync_instrument("CNStock:300750.SZ")
