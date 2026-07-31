import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "internal_task_hard_cutover.py"
SPEC = importlib.util.spec_from_file_location("internal_task_hard_cutover", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_queue_message_decoder_preserves_domain_arguments():
    body = base64.b64encode(json.dumps([["domain-run-1"], {}, {}]).encode()).decode()
    raw = json.dumps({
        "headers": {"id": "task-1", "task": "quantdinger.tasks.cn_fundamental_run"},
        "body": body,
    })
    task = MODULE.decode_queue_message(raw, "maintenance")
    assert task.task_id == "task-1"
    assert task.args == ["domain-run-1"]
    assert MODULE._domain_reference(task) == ("cn_fundamental", "domain-run-1")


def test_queue_inspection_counts_undecodable_messages(monkeypatch):
    monkeypatch.setattr(MODULE, "QUEUES", ("maintenance",))
    monkeypatch.setattr(MODULE, "_container_running", lambda container: True)
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda command, check=False: SimpleNamespace(returncode=0, stdout="not-json\n", stderr=""),
    )
    tasks, lengths, undecodable = MODULE.inspect_queues()
    assert tasks == []
    assert lengths == {"maintenance": 1}
    assert undecodable == {"maintenance": 1}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        json.dumps({"headers": {"task": "quantdinger.tasks.cn_fundamental_run"}, "body": ""}),
        json.dumps({"headers": {"id": "task-1"}, "body": ""}),
    ],
)
def test_queue_decoder_rejects_empty_or_unidentified_messages(raw):
    assert MODULE.decode_queue_message(raw, "maintenance") is None


def test_queue_inspection_does_not_drop_empty_redis_list_items(monkeypatch):
    monkeypatch.setattr(MODULE, "QUEUES", ("maintenance",))
    monkeypatch.setattr(MODULE, "_container_running", lambda container: True)
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda command, check=False: SimpleNamespace(returncode=0, stdout="\n", stderr=""),
    )
    tasks, lengths, undecodable = MODULE.inspect_queues()
    assert tasks == []
    assert lengths == {"maintenance": 1}
    assert undecodable == {"maintenance": 1}


def test_hard_gate_validation_blocks_missing_evidence_and_legacy_work(tmp_path):
    evidence = {gate: True for gate in MODULE.REQUIRED_GATES}
    evidence[MODULE.REQUIRED_GATES[0]] = False
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    failures = MODULE.validate_gates(path, {
        "queueLengths": {"maintenance": 1},
        "undecodableQueueMessages": {"maintenance": 1},
        "tasks": [{"task_id": "active-1"}],
    })
    assert MODULE.REQUIRED_GATES[0] in failures
    assert "legacy_queues_empty" in failures
    assert "legacy_active_reserved_scheduled_empty" in failures
    assert "legacy_queue_messages_undecodable" in failures


def test_hard_gate_validation_passes_complete_evidence(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({gate: True for gate in MODULE.REQUIRED_GATES}))
    assert MODULE.validate_gates(path, {
        "queueLengths": {}, "undecodableQueueMessages": {}, "tasks": [],
    }) == []


def test_cutover_blocks_undecodable_messages_before_side_effects(monkeypatch):
    state = {
        "queueLengths": {"maintenance": 1},
        "undecodableQueueMessages": {"maintenance": 1},
        "tasks": [],
    }
    side_effects = []
    monkeypatch.setattr(MODULE, "snapshot", lambda: state)
    monkeypatch.setattr(MODULE, "_container_running", lambda container: True)
    monkeypatch.setattr(MODULE, "_run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(MODULE, "recovery_manifest", lambda **kwargs: side_effects.append("manifest"))
    monkeypatch.setattr(MODULE, "record_migration", lambda *args, **kwargs: side_effects.append("migration"))
    monkeypatch.setattr(MODULE, "_purge_queues", lambda: side_effects.append("purge"))
    args = SimpleNamespace(
        confirm="TERMINATE_LEGACY_TASKS",
        database_backup_ref="backup-1",
        manifest="manifest.json",
        database_url="postgresql://unused",
    )
    with pytest.raises(RuntimeError, match="legacy_queue_messages_undecodable"):
        MODULE.command_cutover(args)
    assert side_effects == []
