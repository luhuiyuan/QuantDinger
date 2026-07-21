from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from app.services.cn_market_history.config import CNMarketHistorySettings
from app.services.cn_market_history.disk_guard import DiskGuardLevel, DiskGuardStatus
from app.services.cn_market_history.instruments import parse_cn_instrument
from app.services.cn_market_history.models import ProviderProbe, SyncStatus
from app.services.cn_market_history.repository import UpsertSummary
from app.services.cn_market_history.sync_service import CNMarketHistorySyncService
from app.services.cn_market_history.tdx_provider import TDXProvider


def _settings(**overrides) -> CNMarketHistorySettings:
    values = {
        "enabled": False,
        "sync_enabled": True,
        "page_size": 2,
        "write_batch_size": 2,
        "request_interval_seconds": 0.0,
        "provider_timeout_seconds": 5.0,
        "provider_retry_attempts": 1,
        "max_targets_per_run": 10,
        "incremental_lookback_days": 14,
        "daily_symbols": (),
        "disk_path": "/",
        "disk_soft_free_bytes": 5_000,
        "disk_hard_free_bytes": 2_000,
    }
    values.update(overrides)
    return CNMarketHistorySettings(**values)


def _bar(day: int, close: float) -> dict:
    return {
        "year": 2024,
        "month": 1,
        "day": day,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "vol": 1000,
        "amount": 10000,
    }


class _FakeClient:
    pages: dict[int, list[dict]] = {}
    fail_once = False
    calls: list[int] = []

    def __init__(self, host, **kwargs) -> None:
        del kwargs
        self.host = host
        self.connected = False

    @staticmethod
    def ping_all(*, hosts, timeout):
        del timeout
        return [(hosts[1], 0.01), (hosts[0], 0.02)]

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def get_security_bars(self, market, code, category, offset, count):
        del market, code, category, count
        self.calls.append(offset)
        if self.fail_once:
            self.fail_once = False
            raise OSError("temporary")
        return pd.DataFrame(self.pages.get(offset, []))

    def get_xdxr_info(self, market, code):
        del market, code
        return pd.DataFrame(
            [
                {
                    "year": 2024,
                    "month": 1,
                    "day": 3,
                    "category": 1,
                    "name": "dividend",
                    "fenhong": 0.25,
                    "peigujia": None,
                    "songzhuangu": 0.1,
                    "peigu": None,
                    "suogu": None,
                }
            ]
        )


def _api_loader():
    return SimpleNamespace(
        client=_FakeClient,
        market=SimpleNamespace(SH=1, SZ=0),
        day=4,
        known_hosts=lambda: ["host-a", "host-b"],
        retryable=(OSError,),
        version="1.20.4",
    )


def test_provider_paginates_normalizes_and_retries() -> None:
    _FakeClient.calls = []
    _FakeClient.pages = {
        0: [_bar(4, 10.4), _bar(3, 10.3)],
        2: [_bar(2, 10.2), _bar(1, 10.1)],
    }
    _FakeClient.fail_once = True
    provider = TDXProvider(_settings(), api_loader=_api_loader, sleep=lambda _: None)
    instrument = parse_cn_instrument("600519")

    probes = provider.probe_hosts()
    assert provider.selected_host == "host-b"
    assert [probe.healthy for probe in probes] == [True, True]

    with provider:
        pages = list(
            provider.iter_daily_pages(
                instrument,
                date(2024, 1, 2),
                date(2024, 1, 4),
            )
        )
        actions = provider.fetch_corporate_actions(instrument)

    assert _FakeClient.calls == [0, 0, 2]
    assert [bar.trade_date for page in pages for bar in page.bars] == [
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 2),
    ]
    assert pages[-1].reached_start is True
    assert pages[-1].next_offset == 4
    assert actions[0].cash_dividend == Decimal("0.25")
    assert actions[0].bonus_ratio == Decimal("0.1")
    assert actions[0].provider_version == "1.20.4"


def test_provider_accepts_easy_tdx_date_column_and_optional_nan_fields() -> None:
    class DateColumnClient(_FakeClient):
        def get_xdxr_info(self, market, code):
            del market, code
            return pd.DataFrame(
                [
                    {
                        "date": pd.Timestamp("2024-01-03"),
                        "category": 1,
                        "name": "除权除息",
                        "fenhong": 0.25,
                        "peigujia": float("nan"),
                        "songzhuangu": float("nan"),
                        "peigu": float("nan"),
                        "suogu": None,
                    }
                ]
            )

    def date_column_api_loader():
        api = _api_loader()
        api.client = DateColumnClient
        return api

    DateColumnClient.pages = {
        0: [
            {
                "date": pd.Timestamp("2024-01-03"),
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "vol": 1000,
                "amount": 10000,
            }
        ]
    }
    provider = TDXProvider(
        _settings(page_size=2),
        api_loader=date_column_api_loader,
        sleep=lambda _: None,
    )
    instrument = parse_cn_instrument("600519")

    with provider:
        pages = list(
            provider.iter_daily_pages(
                instrument,
                date(2024, 1, 3),
                date(2024, 1, 3),
            )
        )
        actions = provider.fetch_corporate_actions(instrument)

    assert pages[0].bars[0].trade_date == date(2024, 1, 3)
    assert actions[0].event_date == date(2024, 1, 3)
    assert actions[0].cash_dividend == Decimal("0.25")
    assert actions[0].rights_price is None
    assert actions[0].bonus_ratio is None
    assert actions[0].rights_ratio is None


class _FakeOperations:
    def __init__(self) -> None:
        self.target_updates: list[tuple] = []
        self.run_updates: list[tuple] = []
        self.run = {
            "run_id": "run-1",
            "status": "pending",
            "targets": [
                {
                    "instrument": "CNStock:600519.SH",
                    "target_start": date(2024, 1, 2),
                    "target_end": date(2024, 1, 4),
                    "status": "pending",
                    "page_offset": 2,
                    "checkpoint_date": date(2024, 1, 3),
                    "attempts": 1,
                    "bars_written": 2,
                    "actions_written": 0,
                }
            ],
        }

    def get_sync_run(self, run_id):
        assert run_id == "run-1"
        return self.run

    def update_run(self, run_id, status, **fields):
        self.run_updates.append((run_id, status, fields))
        self.run["status"] = status.value

    def update_target(self, run_id, instrument, status, **fields):
        self.target_updates.append((run_id, instrument, status, fields))

    def upsert_provider_probe(self, provider, probe, *, selected):
        del provider, probe, selected


class _FakeDataRepository:
    def __init__(self) -> None:
        self.pages: list[tuple] = []

    def upsert_instrument_metadata(self, metadata, classifications):
        del metadata, classifications

    def upsert_daily_bars(self, bars):
        self.pages.append(tuple(bars))
        return UpsertSummary(inserted=len(bars))

    def upsert_corporate_actions(self, actions):
        return UpsertSummary(inserted=len(actions))

    def invalidate_factor_versions(self, instrument, reason):
        del instrument, reason
        return 0

    def fetch_daily_bars(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return [bar for page in self.pages for bar in page]

    def store_adjustment_factors(self, factors, *, action_data_version):
        del factors, action_data_version


class _FakeSyncProvider:
    selected_host = "host-a"

    def __init__(self, settings) -> None:
        del settings
        self.start_offsets: list[int] = []

    def probe_hosts(self):
        return [
            ProviderProbe(
                host="host-a",
                latency_ms=1.0,
                healthy=True,
                checked_at=datetime.now(timezone.utc),
            )
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        del args

    def iter_daily_pages(self, instrument, start_date, end_date, *, start_offset):
        del start_date, end_date
        self.start_offsets.append(start_offset)
        bar = SimpleNamespace(trade_date=date(2024, 1, 2))
        yield SimpleNamespace(
            offset=start_offset,
            next_offset=start_offset + 1,
            raw_count=1,
            bars=(bar,),
        )

    def fetch_instrument_metadata(self, instrument):
        return SimpleNamespace(instrument=instrument), ()

    def fetch_corporate_actions(self, instrument):
        del instrument
        return []


class _FakeDiskGuard:
    def __init__(self, level=DiskGuardLevel.OK) -> None:
        self.level = level

    def check(self):
        return DiskGuardStatus(
            path="/",
            level=self.level,
            total_bytes=10_000,
            used_bytes=1_000,
            free_bytes=9_000 if self.level is DiskGuardLevel.OK else 1_000,
            soft_free_bytes=5_000,
            hard_free_bytes=2_000,
        )


class _FakeQualityService:
    def validate_bars(self, instrument, bars):
        del instrument, bars
        return ()

    def validate_actions(self, instrument, actions):
        del instrument, actions
        return ()

    def assess(self, instrument, start_date, end_date, *, provider):
        del instrument, start_date, end_date, provider
        return SimpleNamespace(report=SimpleNamespace(complete=False))


@contextmanager
def _acquired_lock(key):
    assert key in {"cn-history:global-sync", "cn-history:CNStock:600519.SH"}
    yield True


def test_sync_resumes_from_checkpoint_and_writes_sequentially() -> None:
    operations = _FakeOperations()
    data = _FakeDataRepository()
    providers: list[_FakeSyncProvider] = []

    def provider_factory(settings):
        provider = _FakeSyncProvider(settings)
        providers.append(provider)
        return provider

    service = CNMarketHistorySyncService(
        settings=_settings(),
        data_repository=data,
        operations_repository=operations,
        provider_factory=provider_factory,
        disk_guard=_FakeDiskGuard(),
        lock_factory=_acquired_lock,
        quality_service=_FakeQualityService(),
    )

    result = service.run("run-1")

    assert result["status"] == SyncStatus.SUCCEEDED.value
    assert providers[0].start_offsets == [2]
    assert len(data.pages) == 1
    succeeded = [item for item in operations.target_updates if item[2] is SyncStatus.SUCCEEDED]
    assert succeeded[0][3]["page_offset"] == 3
    assert succeeded[0][3]["bars_written"] == 3


def test_sync_pauses_before_writing_at_hard_disk_limit() -> None:
    operations = _FakeOperations()
    data = _FakeDataRepository()
    service = CNMarketHistorySyncService(
        settings=_settings(),
        data_repository=data,
        operations_repository=operations,
        provider_factory=_FakeSyncProvider,
        disk_guard=_FakeDiskGuard(DiskGuardLevel.HARD),
        lock_factory=_acquired_lock,
        quality_service=_FakeQualityService(),
    )

    result = service.run("run-1")

    assert result["status"] == SyncStatus.PAUSED.value
    assert data.pages == []
    paused = [item for item in operations.target_updates if item[2] is SyncStatus.PAUSED]
    assert paused[0][3]["page_offset"] == 2
    assert paused[0][3]["last_error_code"] == "cn_history.disk_hard_limit"


def test_retry_excludes_parent_overlap_and_writes_sanitized_audit_scope() -> None:
    class Operations:
        def __init__(self):
            self.overlap_calls = []
            self.audits = []

        def get_sync_run(self, run_id):
            assert run_id == "run-parent"
            return {
                "run_id": run_id,
                "status": "paused",
                "targets": [
                    {
                        "instrument": "CNStock:600519.SH",
                        "target_start": date(2024, 1, 2),
                        "target_end": date(2024, 1, 4),
                        "status": "paused",
                    }
                ],
            }

        def find_overlapping_active_run(
            self, instruments, start_date, end_date, *, exclude_run_id
        ):
            self.overlap_calls.append(
                (instruments, start_date, end_date, exclude_run_id)
            )
            return None

        def create_sync_run(self, targets, **kwargs):
            assert kwargs["parent_run_id"] == "run-parent"
            return "run-retry"

        def write_audit(self, **kwargs):
            self.audits.append(kwargs)

    operations = Operations()
    service = CNMarketHistorySyncService(
        settings=_settings(),
        operations_repository=operations,
        disk_guard=_FakeDiskGuard(),
    )

    run_id = service.retry_failed_run("run-parent", requested_by=7)

    assert run_id == "run-retry"
    assert operations.overlap_calls[0][-1] == "run-parent"
    audit = operations.audits[-1]
    assert audit["action"] == "retry_sync"
    assert audit["actor_user_id"] == 7
    assert audit["request_scope"] == {
        "instrument_count": 1,
        "start_date": "2024-01-02",
        "end_date": "2024-01-04",
        "request_kind": "retry",
        "parent_run_id": "run-parent",
    }
    assert not any(
        key in str(audit).lower() for key in ("password", "api_key", "secret")
    )
