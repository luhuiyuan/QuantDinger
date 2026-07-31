## Why

当前 Celery 任务缺少与 QuantDinger 领域运行状态一致的进度、检查点、错误详情和前端运维能力，长时间基本面回填还曾因全局 Celery 超时被终止并遗留错误的运行状态。项目需要在现有低资源单机部署中建立可审计、可取消、可重试且可视化管理的内部有限任务系统，并在全部验证门槛通过后彻底移除 Celery 相关组件。

## What Changes

- 在现有 `scheduler-worker` 进程角色中新增内部 Task Scheduler 和 Task Run executor，不新增 Docker 容器，并保持其与长生命周期 Domain Scheduler 的职责边界。
- 新增代码注册、版本化的 Task Definition，以及持久化的计划、运行、租约、结构化事件和管理审计控制层；通用控制记录通过领域引用与现有同步批次、回填批次和 Agent Job 等领域运行表并存。
- 支持标准五字段 Cron、计划时区、5 秒扫描、分钟级幂等、停机漏触发跳过、互斥键、防重叠、固定优先级和 FIFO；第一阶段全局执行并发为 1。
- 每个 Task Run 使用独立子进程执行；采用数据库 advisory lock、60 秒 Run lease、20 秒心跳和 180 秒失联判定，并支持任务级超时、检查点式安全取消、临时错误自动重试和管理员人工重试。
- 新增统一 Task Management 页面及管理 API，包含 Overview、Schedules、Runs、Logs 四个标签页，支持启动、暂停未来计划、修改 Cron、取消、重试、查看进度和脱敏结构化事件；第一版每 5 秒轮询并为后续 SSE 保留 cursor 语义。
- 按任务定义执行单一归属的分阶段迁移；迁移切换时立即终止现有 Celery 任务，统一记录 `migration_interrupted`，只允许管理员检查检查点后人工创建新 Run。
- **BREAKING**：全部任务切换且所有硬验证门槛通过后，从部署、代码、配置、健康检查和依赖中删除 Celery Worker、Celery Beat、`redis-jobs` 及 Celery 专用实现。
- **BREAKING**：运行中任务不再支持暂停/恢复；仅计划可暂停，运行中任务使用安全取消，并通过新 Run 进行重试。

## Capabilities

### New Capabilities

- `internal-task-orchestration`: 代码注册任务、Cron 调度、持久化 Run 生命周期、租约、隔离执行、互斥、重试、超时和检查点式取消。
- `task-operations-console`: 面向管理员和任务所有者的任务查询、计划管理、运行控制、结构化事件、进度、审计和 Web 管理页面。
- `celery-retirement`: 按任务定义迁移、迁移中断处理、硬门槛验证以及 Celery 运行组件和依赖的最终删除。

### Modified Capabilities

- `cn-market-data-operations`: 基本面与行情同步批次改由内部 Task Run 控制；移除运行中暂停/恢复，改为安全取消和基于检查点的新 Run 重试，并明确 Cron 重叠时跳过。

## Impact

- 后端：`backend_api_python/app/tasks/`、`app/celery_app.py`、现有 scheduler 命令、市场数据同步服务、Agent Job 与 fast analysis 投递入口、任务健康检查、管理 API、数据库模型和迁移。
- 前端：`/home/quantadinger/QuantDinger-Vue` 新增统一 Task Management 页面、路由、API 客户端、权限控制和多语言文案。
- 部署：调整 `docker-compose.yml`、GHCR Compose、环境变量、readiness/health 检查和运维文档；最终删除 Celery Worker、Celery Beat 和 `redis-jobs`，保留仍承担缓存职责的 Redis。
- 数据库：新增 `qd_task_definitions`、`qd_task_schedules`、`qd_task_runs`、`qd_task_events`、`qd_task_leases`、`qd_task_audit` 及必要约束、索引和领域引用；保留现有领域运行表。
- 测试：新增注册表、状态机、Cron、幂等与互斥、租约失效、子进程、重试、取消、权限、审计、迁移门槛、Compose 和前端交互的针对性测试。
