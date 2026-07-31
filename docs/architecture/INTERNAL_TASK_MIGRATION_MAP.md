# Internal Task Migration Map

本文档是硬切换实施前的任务盘点。分阶段表示开发和验收顺序，不表示生产运行期保留 Celery/内部双后端；最终发布直接移除 Celery 运行组件和代码。

## 运行角色

| 当前入口 | 当前行为 | 内部 Task Definition | 领域归属 | 阶段 |
| --- | --- | --- | --- | --- |
| `app.tasks.maintenance.record_worker_heartbeat` | Celery Beat 每 10 秒 | `worker_heartbeat` | `qd_worker_heartbeats` | 1 |
| `app.tasks.maintenance.cleanup_runtime_metadata` | Celery Beat 每日 | `runtime_metadata_cleanup` | runtime metadata | 1 |
| `app.tasks.maintenance.cleanup_external_data_request_logs` | Celery Beat 每日 | `external_data_request_log_cleanup` | cleanup run/audit | 1 |
| `app.tasks.maintenance.run_reflection` | Celery Beat 周期任务 | `reflection_cycle` | reflection domain result | 1 |
| `app.tasks.maintenance.run_ai_calibration` | Celery Beat 周期任务 | `ai_calibration_cycle` | calibration result | 2 |
| `app.tasks.maintenance.run_market_catalog_sync` | Celery Beat 周期任务 | `market_catalog_sync` | catalog sync result | 1 |
| `app.tasks.maintenance.run_cn_market_history_sync` | `send_task` 按批次投递 | `cn_market_history_sync` | `qd_cn_history_sync_runs/targets` | 1 |
| `app.tasks.maintenance.run_cn_market_history_daily` | Celery Beat 每日 | `cn_market_history_daily` | `qd_cn_history_sync_runs/targets` | 1 |
| `app.tasks.maintenance.run_cn_stock_quote_refresh` | Celery Beat 每 5 分钟 | `cn_stock_quote_refresh` | quote refresh result | 1 |
| `app.tasks.maintenance.run_cn_fundamental_incremental` | Celery Beat 20:30 | `cn_fundamental_incremental` | `qd_cn_fundamental_sync_runs/targets` | 1 |
| `app.tasks.maintenance.run_cn_fundamental_run` | `send_task` 按批次投递 | `cn_fundamental_backfill` | `qd_cn_fundamental_sync_runs/targets` | 1 |
| `app.tasks.agent_jobs.execute_agent_job` | Celery `jobs` queue | `agent_backtest` | `qd_agent_jobs` | 2 |
| `app.tasks.fast_analysis.execute_fast_analysis` | Celery `ai` queue或线程 fallback | `fast_ai_analysis` | analysis memory/billing | 2 |

## 需要移除的投递与运行入口

- `app.celery_app`、Celery task decorators、Beat schedule 和 task routes。
- `cn_market_history_admin.py` 中的 `celery_app.send_task`。
- `app/utils/agent_jobs.py` 中的 `CELERY_TASKS_ENABLED` 分支和线程 fallback；提交后统一创建内部 Task Run。
- `app/services/fast_analysis_tasks.py` 中的 Redis/Celery inflight 与线程 fallback；互斥和执行改由 Task Definition/Run 控制。
- readiness、worker health、metrics、process role 中的 Celery broker/role 检查。
- Compose 中的 Celery Worker、Celery Beat、`redis-jobs`、Celery 专用 volume、依赖和环境变量。

## 硬切换前置清单

1. 上表所有任务的内部适配、领域检查点和权限测试完成。
2. Celery active/reserved/scheduled 队列为空或已按迁移流程撤回。
3. 活动 Celery 任务已终止并记录 `migration_interrupted`，不自动创建内部 Run。
4. 内部 Task Management 页面和 Domain Scheduler 健康检查通过。
5. 通过 OpenSpec 中列出的全部硬门槛后，发布不含 Celery 代码和容器的版本。
