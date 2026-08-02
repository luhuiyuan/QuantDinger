from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.data_routing.cutover import CutoverBlockedError, CutoverState, DataRoutingCutoverService, MANDATORY_CUTOVER_GATES
from app.services.data_routing.legacy_import import LegacyCredentialDetector, LegacyCredentialImportError, LegacyCredentialImportService
from app.services.data_routing.preflight import CutoverEvidenceCollector


class CutoverRepo:
    def __init__(self): self.state = CutoverState(1, "4.1.0", "preflight", ())
    def create_preflight(self, target_version, **kwargs): self.state = replace(self.state, target_version=target_version); return self.state
    def save_gates(self, cutover_id, gates, **kwargs): self.state = replace(self.state, gates=gates); return self.state
    def transition(self, cutover_id, expected_status, new_status, **kwargs):
        assert self.state.status == expected_status
        self.state = replace(self.state, status=new_status)
        return self.state


def test_cutover_requires_every_gate_and_drain_then_is_forward_only():
    repo = CutoverRepo(); service = DataRoutingCutoverService(repo)
    state = service.begin_preflight("4.1.0", actor_user_id=1, reason="release")
    state = service.evaluate(state, {key: True for key in MANDATORY_CUTOVER_GATES}, actor_user_id=1)
    state = service.enter_maintenance(state, actor_user_id=1)
    with pytest.raises(CutoverBlockedError): service.activate(state, actor_user_id=1, active_attempts=1, active_streams=0, workers_stopped=True)
    state = service.activate(state, actor_user_id=1, active_attempts=0, active_streams=0, workers_stopped=True)
    with pytest.raises(CutoverBlockedError): service.abort(state, actor_user_id=1, reason="rollback")


def test_failed_gate_cannot_enter_maintenance():
    repo = CutoverRepo(); service = DataRoutingCutoverService(repo)
    state = service.evaluate(repo.state, {key: key != "inventory_zero_bypass" for key in MANDATORY_CUTOVER_GATES}, actor_user_id=1)
    with pytest.raises(CutoverBlockedError): service.enter_maintenance(state, actor_user_id=1)


class ImportRepo:
    def __init__(self): self.status = {}
    def import_statuses(self): return self.status
    def record_import(self, adapter_key, instance_id, **kwargs): self.status[adapter_key] = ("imported", instance_id)
    def record_failure(self, *args, **kwargs): pass


def test_legacy_detector_never_returns_secret_and_import_is_one_time():
    repo = ImportRepo(); detector = LegacyCredentialDetector({"FINNHUB_API_KEY": "secret-value"}, repo)
    detection = next(item for item in detector.detect() if item.adapter_key == "finnhub")
    assert detection.detected is True and "secret-value" not in repr(detection)
    instances = SimpleNamespace(create_draft=lambda **kwargs: SimpleNamespace(instance_id=9))
    credentials = SimpleNamespace(submit_and_validate=lambda *args, **kwargs: None)
    service = LegacyCredentialImportService(detector, repo, instances, credentials)
    assert service.import_adapter("finnhub", actor_user_id=1, reason="migration").instance_id == 9
    with pytest.raises(LegacyCredentialImportError): service.import_adapter("finnhub", actor_user_id=1, reason="again")


def test_server_owned_preflight_evidence_reports_inventory_blocker(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"entries":[{"scope":"in_scope","path":"old.py","migration_status":"legacy_unmigrated","router_entries":[]}]}')
    matrix = tmp_path / "matrix.json"
    matrix.write_text('{"passed":true,"artifact_version":1}')
    repository = SimpleNamespace(control_plane_evidence=lambda: {
        "capability_preflight": {"passed": True}, "healthy_instances": {"passed": True},
        "effective_policies": {"passed": True}, "explicit_disables": {"passed": True},
    })
    evidence = CutoverEvidenceCollector(
        repository, inventory_path=inventory, test_matrix_path=matrix,
        credential_key_check=lambda: {"passed": True}, background_check=lambda: {"passed": True},
    ).collect()
    assert evidence["inventory_zero_bypass"]["passed"] is False
    assert evidence["go_no_go_report"] == {"passed": False, "decision": "NO_GO", "blockers": ["inventory_zero_bypass"]}


def test_server_owned_preflight_reports_environment_failures_as_no_go(tmp_path):
    inventory = tmp_path / "missing-inventory.json"
    matrix = tmp_path / "matrix.json"
    matrix.write_text('{"passed":true,"artifact_version":1}')

    class UnavailableRepository:
        def control_plane_evidence(self):
            raise RuntimeError("database details must not escape")

    evidence = CutoverEvidenceCollector(
        UnavailableRepository(), inventory_path=inventory, test_matrix_path=matrix,
        credential_key_check=lambda: {"passed": False, "reason": "not configured"},
        background_check=lambda: {"passed": True},
    ).collect()

    assert evidence["inventory_zero_bypass"]["error_type"] == "FileNotFoundError"
    assert evidence["capability_preflight"] == {
        "passed": False,
        "reason": "control plane evidence is unavailable",
        "error_type": "RuntimeError",
    }
    assert evidence["go_no_go_report"] == {
        "passed": False,
        "decision": "NO_GO",
        "blockers": [
            "inventory_zero_bypass",
            "capability_preflight",
            "healthy_instances",
            "effective_policies",
            "explicit_disables",
            "credential_key_readiness",
        ],
    }


def test_packaged_cutover_artifacts_match_repository_documents():
    backend_root = Path(__file__).resolve().parents[1]
    repository_root = backend_root.parent
    packaged = backend_root / "data_routing_artifacts"
    documented = repository_root / "docs" / "architecture"

    for name in ("EXTERNAL_DATA_OUTBOUND_INVENTORY.json", "DATA_ROUTING_TEST_MATRIX.json"):
        assert json.loads((packaged / name).read_text(encoding="utf-8")) == json.loads(
            (documented / name).read_text(encoding="utf-8")
        )
