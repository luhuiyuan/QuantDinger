"""Scheduler process entrypoint."""

from __future__ import annotations

import os
import threading


def main() -> None:
    os.environ["QD_PROCESS_ROLE"] = "scheduler"

    from app import create_app
    from app.runtime.process import ShutdownSignal
    from app.startup import _start_scheduler_services
    from app.services.strategy_command_repository import StrategyCommandRepository
    from app.services.task_control.registry import default_task_registry, sync_registered_task_definitions
    from app.services.task_control.builtin_tasks import register_phase_one_tasks, register_phase_two_tasks
    from app.services.task_control.builtin_schedules import BUILTIN_SCHEDULES
    from app.services.task_control.repository import TaskControlRepository
    from app.services.task_control.scheduler import TaskScheduler
    from app.services.task_control.executor import TaskExecutor
    from app.utils.logger import get_logger
    from app.workers.trading import build_worker_id

    app = create_app(register_http_routes=False)
    shutdown = ShutdownSignal()
    shutdown.install()
    repository = StrategyCommandRepository()
    worker_id = build_worker_id()
    logger = get_logger(__name__)
    lease_key = "scheduler-global-services"
    lease_seconds = max(10, int(os.getenv("SCHEDULER_LEASE_SEC", "30")))
    leader = False
    task_scheduler = None
    task_executor = None
    task_executor_thread = None
    task_executor_stop = threading.Event()
    with app.app_context():
        try:
            register_phase_one_tasks(default_task_registry)
            register_phase_two_tasks(default_task_registry)
            if default_task_registry.all():
                task_repository = TaskControlRepository()
                sync_registered_task_definitions(task_repository)
                task_repository.ensure_builtin_schedules(BUILTIN_SCHEDULES)

                def _exclusivity_key(decision):
                    definition = default_task_registry.get(decision.task_key)
                    return definition.exclusivity_key(decision.parameters)

                task_scheduler = TaskScheduler(repository=task_repository, exclusivity_key=_exclusivity_key)
                task_executor = TaskExecutor(repository=task_repository, registry=default_task_registry, holder_id=worker_id)
            else:
                logger.warning("Task Registry is empty; finite Task Scheduler remains disabled")
            while not shutdown.event.is_set():
                if not leader:
                    leader = repository.acquire_process_lease(
                        lease_key=lease_key,
                        owner_id=worker_id,
                        lease_seconds=lease_seconds,
                    )
                    if leader:
                        _start_scheduler_services()
                        if task_executor is not None and task_executor_thread is None:
                            def _executor_loop() -> None:
                                while not task_executor_stop.is_set() and not shutdown.event.is_set():
                                    try:
                                        result = task_executor.execute_one()
                                        if result is None:
                                            task_executor_stop.wait(1)
                                    except Exception:
                                        logger.error("Task executor iteration failed", exc_info=True)
                                        task_executor_stop.wait(1)

                            task_executor_thread = threading.Thread(
                                target=_executor_loop,
                                name="TaskExecutorParent",
                                daemon=False,
                            )
                            task_executor_thread.start()
                else:
                    leader = repository.renew_process_lease(
                        lease_key=lease_key,
                        owner_id=worker_id,
                        lease_seconds=lease_seconds,
                    )
                    if not leader:
                        # Fence new scheduling, then synchronously stop the executor
                        # before allowing another scheduler to acquire leadership.
                        task_executor_stop.set()
                        if task_executor is not None:
                            task_executor.stop()
                        break
                if leader and task_scheduler is not None:
                    try:
                        task_scheduler.scan_once()
                    except Exception:
                        logger.error("Task Scheduler scan failed", exc_info=True)
                repository.record_worker_heartbeat(
                    worker_id=worker_id,
                    role="scheduler",
                    metadata={
                        "leader": leader,
                        "domain_scheduler": "running" if leader else "standby",
                        "task_scheduler": "running" if leader and task_scheduler is not None else "not_ready",
                        "task_executor": (
                            "running" if task_executor_thread is not None and task_executor_thread.is_alive()
                            else "not_ready"
                        ),
                    },
                )
                shutdown.event.wait(5)
        finally:
            task_executor_stop.set()
            if task_executor_thread is not None:
                task_executor_thread.join()
            if leader:
                repository.release_process_lease(lease_key=lease_key, owner_id=worker_id)
            repository.mark_worker_stopped(worker_id)


if __name__ == "__main__":
    main()
