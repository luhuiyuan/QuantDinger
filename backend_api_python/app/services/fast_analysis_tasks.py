"""Async task and in-flight helpers for fast analysis routes."""

import threading
import time

from app.utils.logger import get_logger

logger = get_logger(__name__)

_analysis_inflight_lock = threading.Lock()
_analysis_inflight: dict[str, float] = {}
_distributed_inflight: dict[tuple[int, str], object] = {}


def build_inflight_key(user_id: int, market: str, symbol: str, timeframe: str) -> str:
    return (
        f"{int(user_id)}|{str(market or '').strip().upper()}|"
        f"{str(symbol or '').strip().upper()}|{str(timeframe or '').strip().upper()}"
    )


def build_task_exclusivity_key(user_id: int, market: str, symbol: str, timeframe: str) -> str:
    return (
        f"fast-analysis:{int(user_id)}:{str(market or '').strip().upper()}:"
        f"{str(symbol or '').strip().upper()}:{str(timeframe or '').strip().upper()}"
    )


def acquire_inflight(key: str, ttl_sec: int = 90, *, distributed: bool = False) -> bool:
    if distributed:
        from app.services.task_control.repository import TaskControlRepository

        context = TaskControlRepository().submission_lock(key)
        acquired = bool(context.__enter__())
        if not acquired:
            context.__exit__(None, None, None)
            return False
        with _analysis_inflight_lock:
            _distributed_inflight[(threading.get_ident(), key)] = context
        return True
    now = time.time()
    with _analysis_inflight_lock:
        stale = [k for k, exp in _analysis_inflight.items() if float(exp) <= now]
        for stale_key in stale[:1024]:
            _analysis_inflight.pop(stale_key, None)
        if key in _analysis_inflight and float(_analysis_inflight.get(key) or 0) > now:
            return False
        _analysis_inflight[key] = now + int(ttl_sec)
        return True


def release_inflight(key: str) -> None:
    with _analysis_inflight_lock:
        context = _distributed_inflight.pop((threading.get_ident(), key), None)
    if context is not None:
        context.__exit__(None, None, None)
        return
    with _analysis_inflight_lock:
        _analysis_inflight.pop(key, None)


def try_refund_credits(
    user_id: int,
    amount: int,
    remark: str,
    reference_id: str = "",
) -> None:
    """Best-effort refund when analysis fails after a pre-charge."""
    try:
        if int(amount or 0) <= 0:
            return
        from app.services.billing_service import get_billing_service

        get_billing_service().add_credits(
            user_id=int(user_id),
            amount=int(amount),
            action="refund",
            remark=remark,
            reference_id=reference_id,
        )
    except Exception as exc:
        logger.error("Async auto refund failed: %s", exc, exc_info=True)


def run_async_analysis_task(
    task_memory_id: int,
    market: str,
    symbol: str,
    language: str,
    model: str,
    timeframe: str,
    user_id: int,
    inflight_key: str,
    credits_charged: int = 0,
) -> dict:
    """Execute analysis in a background worker and finalize pending history."""
    try:
        from app.services.analysis_memory import get_analysis_memory
        from app.services.fast_analysis import get_fast_analysis_service

        service = get_fast_analysis_service()
        memory = get_analysis_memory()
        result = service.analyze(
            market=market,
            symbol=symbol,
            language=language,
            model=model,
            timeframe=timeframe,
            user_id=user_id,
        )
        memory.finalize_pending_task(task_memory_id, result)
        if result.get("error"):
            try_refund_credits(
                user_id=int(user_id),
                amount=int(credits_charged or 0),
                remark=f"Auto refund: async fast-analysis failed ({market}:{symbol}:{timeframe})",
                reference_id=f"fast-analysis-refund:{int(task_memory_id)}",
            )

        auto_memory_id = result.get("memory_id")
        if auto_memory_id and int(auto_memory_id) != int(task_memory_id):
            try:
                memory.delete_history(int(auto_memory_id), user_id=user_id)
            except Exception:
                pass
        return result
    except Exception as exc:
        logger.error("Async analysis task failed: %s", exc, exc_info=True)
        try_refund_credits(
            user_id=int(user_id),
            amount=int(credits_charged or 0),
            remark=f"Auto refund: async fast-analysis exception ({market}:{symbol}:{timeframe})",
            reference_id=f"fast-analysis-refund:{int(task_memory_id)}",
        )
        try:
            from app.services.analysis_memory import get_analysis_memory

            get_analysis_memory().fail_pending_task(task_memory_id, str(exc))
        except Exception:
            pass
        raise
    finally:
        try:
            release_inflight(inflight_key)
        except Exception:
            pass


def start_async_analysis_task(*args, **kwargs):
    names = ("task_memory_id", "market", "symbol", "language", "model", "timeframe", "user_id", "inflight_key", "credits_charged")
    parameters = dict(zip(names, args))
    parameters.update(kwargs)
    parameters.setdefault("model", "")
    parameters.setdefault("credits_charged", 0)
    from app.services.task_control.builtin_tasks import register_phase_two_tasks
    from app.services.task_control.registry import default_task_registry
    from app.services.task_control.repository import TaskControlRepository

    register_phase_two_tasks(default_task_registry)
    definition = default_task_registry.get("fast_ai_analysis")
    validated = definition.validate_parameters(parameters)
    run, _created = TaskControlRepository().create_run(
        task_key=definition.task_key, definition_version=definition.definition_version,
        exclusivity_key=definition.exclusivity_key(validated, int(validated["user_id"])), parameters=validated,
        priority=definition.priority, owner_user_id=int(validated["user_id"]), domain_kind="fast_analysis",
        domain_run_id=str(validated["task_memory_id"]),
    )
    if not _created:
        raise RuntimeError(f"Fast analysis Task Run already active: {run.run_id}")
    return run
