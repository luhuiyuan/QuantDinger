from pathlib import Path

from app.services.task_control.builtin_tasks import register_phase_one_tasks
from app.services.task_control import builtin_tasks
from app.services.task_control.builtin_schedules import BUILTIN_SCHEDULES
from app.services.task_control.registry import TaskRegistry


def test_phase_one_registry_contains_market_and_maintenance_tasks():
    registry = TaskRegistry()
    register_phase_one_tasks(registry)
    keys = {definition.task_key for definition in registry.all()}
    assert "cn_fundamental_backfill" in keys
    assert "cn_fundamental_incremental" in keys
    assert "cn_market_history_sync" in keys
    assert "external_data_request_log_cleanup" in keys
    assert "task_event_cleanup" in keys
    assert "provider_credential_reencrypt" in keys
    assert "provider_capability_revalidation" in keys


def test_provider_credential_tasks_are_globally_serial_and_bounded():
    registry = TaskRegistry()
    register_phase_one_tasks(registry)
    rotation = registry.get("provider_credential_reencrypt")
    revalidation = registry.get("provider_capability_revalidation")
    assert rotation.max_concurrency == 1
    assert rotation.default_parameters == {"batch_size": 25, "max_batches": 10}
    assert rotation.exclusivity_key({}, None) == "provider_credential_reencrypt"
    assert revalidation.max_concurrency == 1
    assert revalidation.default_parameters == {"limit": 10}


def test_backfill_definition_has_no_total_timeout_capability():
    registry = TaskRegistry()
    register_phase_one_tasks(registry)
    definition = registry.get("cn_fundamental_backfill")
    assert definition.capabilities["no_total_timeout"] is True
    assert definition.capabilities["cancel_grace_seconds"] == 60
    assert definition.cancellation_handler is not None
    assert definition.manual_retry_handler is not None


def test_builtin_schedules_use_five_field_cron_and_china_timezone():
    assert {item.task_key for item in BUILTIN_SCHEDULES} >= {
        "cn_stock_quote_refresh", "cn_fundamental_incremental", "task_event_cleanup",
        "provider_capability_revalidation",
    }
    assert all(len(item.cron_expression.split()) == 5 for item in BUILTIN_SCHEDULES)
    assert all(item.timezone_name == "Asia/Shanghai" for item in BUILTIN_SCHEDULES)


def test_builtin_schedule_sync_is_insert_only_and_preserves_revisions():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "task_control" / "repository.py").read_text()
    section = source[source.index("def ensure_builtin_schedules"):source.index("def list_definitions")]
    assert "WHERE NOT EXISTS" in section
    assert "UPDATE qd_task_schedules" not in section


def test_scheduled_revalidation_invokes_the_production_registry_loader(monkeypatch):
    from app.services.data_routing import bootstrap
    from app.services.data_routing.registry import DataRoutingRegistry

    calls = []
    monkeypatch.setattr(
        bootstrap,
        "load_default_data_routing_registry",
        lambda: calls.append(True) or DataRoutingRegistry(),
    )

    result = builtin_tasks._revalidate_provider_capabilities({}, reporter=None)

    assert calls == [True]
    assert result == {"skipped": True, "reason": "data_routing_registry_empty"}
