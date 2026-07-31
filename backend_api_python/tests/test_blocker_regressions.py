from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_scheduler_fences_executor_before_releasing_leadership():
    source = (ROOT / "app/commands/scheduler.py").read_text()
    assert "task_executor.stop()" in source
    assert "task_executor_thread.join()" in source
    assert "daemon=False" in source


def test_child_has_parent_death_and_process_group_fencing():
    source = (ROOT / "app/services/task_control/executor.py").read_text()
    assert "PR_SET_PDEATHSIG" in source
    assert "os.killpg" in source


def test_executor_finalizes_linked_work_on_non_retryable_termination():
    source = (ROOT / "app/services/task_control/executor.py").read_text()
    assert "def finalize_linked_work" in source
    assert 'finalize_linked_work("Task timed out")' in source
    assert 'finalize_linked_work("Task was forcibly interrupted")' in source


def test_cutover_stops_producers_before_snapshot_and_records_after_termination():
    source = (ROOT.parent / "scripts/internal_task_hard_cutover.py").read_text()
    assert "Stop new producers before taking the authoritative snapshot" in source
    assert source.index("after = snapshot(require_worker=False)") < source.rindex("record_migration(tasks")


def test_prometheus_has_only_cache_redis_target():
    source = (ROOT.parent / "ops/prometheus/prometheus.yml").read_text()
    alerts = (ROOT.parent / "ops/prometheus/alerts.yml").read_text()
    assert "redis-jobs" not in source
    assert 'job="redis-cache"' in alerts
