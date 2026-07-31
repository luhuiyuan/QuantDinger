import pytest

from app.services.task_control.repository import (
    TERMINAL_RUN_STATUSES,
    TaskRunRecord,
    validate_run_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "retry_wait"),
        ("running", "cancel_requested"),
        ("cancel_requested", "cancelled"),
        ("retry_wait", "queued"),
    ],
)
def test_valid_task_run_transitions(current, target):
    validate_run_transition(current, target)


@pytest.mark.parametrize("status", sorted(TERMINAL_RUN_STATUSES))
def test_terminal_task_run_statuses_are_immutable(status):
    with pytest.raises(ValueError, match="Illegal Task Run transition"):
        validate_run_transition(status, "queued")


def test_running_cannot_jump_directly_to_cancelled():
    with pytest.raises(ValueError, match="running -> cancelled"):
        validate_run_transition("running", "cancelled")


def test_task_run_record_decodes_json_snapshots():
    record = TaskRunRecord.from_row(
        {
            "run_id": "run-1",
            "task_key": "catalog.sync",
            "definition_version": "1",
            "status": "queued",
            "exclusivity_key": "catalog.sync",
            "parameters": '{"market":"CN"}',
            "result_summary": '{"count":2}',
            "priority": 2,
        }
    )

    assert record.parameters == {"market": "CN"}
    assert record.result_summary == {"count": 2}
