from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.data_routing.legacy_import import (
    LegacyCredentialDetector,
    LegacyCredentialImportError,
    LegacyCredentialImportService,
)


class Repository:
    def __init__(self, statuses=None):
        self.statuses = dict(statuses or {})
        self.imports = []
        self.failures = []

    def import_statuses(self): return self.statuses
    def record_import(self, *args, **kwargs): self.imports.append((args, kwargs))
    def record_failure(self, *args, **kwargs): self.failures.append((args, kwargs))


class Instances:
    def create_draft(self, **kwargs): return SimpleNamespace(instance_id=17)


class Credentials:
    def __init__(self, error=None): self.error = error; self.seen = None
    def submit_and_validate(self, instance_id, credentials, **kwargs):
        self.seen = (instance_id, credentials, kwargs)
        if self.error: raise self.error


def test_detector_exposes_source_names_but_never_secret_values():
    detections = LegacyCredentialDetector({"FINNHUB_API_KEY": "top-secret"}).detect()
    finnhub = next(item for item in detections if item.adapter_key == "finnhub")
    assert finnhub.detected is True
    assert finnhub.source_names == ("FINNHUB_API_KEY",)
    assert "top-secret" not in repr(detections)


def test_import_encrypts_through_credential_service_and_records_sanitized_source():
    repository, credentials = Repository(), Credentials()
    service = LegacyCredentialImportService(
        LegacyCredentialDetector({"FINNHUB_API_KEY": "top-secret"}, repository),
        repository, Instances(), credentials,
    )
    result = service.import_adapter("finnhub", actor_user_id=3, reason="migrate")
    assert result.import_status == "imported" and result.instance_id == 17
    assert credentials.seen[1] == {"api_key": "top-secret"}
    assert repository.imports[0][1]["source_names"] == ("FINNHUB_API_KEY",)


def test_failed_validation_records_repairable_failed_status_without_secret():
    repository = Repository()
    service = LegacyCredentialImportService(
        LegacyCredentialDetector({"FINNHUB_API_KEY": "top-secret"}, repository),
        repository, Instances(), Credentials(ValueError("provider rejected credential")),
    )
    with pytest.raises(ValueError):
        service.import_adapter("finnhub", actor_user_id=3, reason="migrate")
    assert repository.failures[0][0] == ("finnhub", 17)
    assert repository.failures[0][1]["source_names"] == ("FINNHUB_API_KEY",)
    assert "top-secret" not in repr(repository.failures)


def test_imported_source_is_one_time_but_failed_source_can_be_retried():
    imported = Repository({"finnhub": ("imported", 17)})
    with pytest.raises(LegacyCredentialImportError):
        LegacyCredentialImportService(
            LegacyCredentialDetector({"FINNHUB_API_KEY": "x"}, imported),
            imported, Instances(), Credentials(),
        ).import_adapter("finnhub", actor_user_id=3, reason="again")

    failed = Repository({"finnhub": ("failed", 17)})
    result = LegacyCredentialImportService(
        LegacyCredentialDetector({"FINNHUB_API_KEY": "new"}, failed),
        failed, Instances(), Credentials(),
    ).import_adapter("finnhub", actor_user_id=3, reason="repair")
    assert result.import_status == "imported"
