## 1. 基线与迁移护栏

- [x] 1.1 盘点 `backend_api_python/app/tasks/`、`send_task`、Agent Job、fast analysis、Beat 配置、健康检查和 Compose 中全部 Celery 入口，建立 Task Definition 与阶段映射清单
- [ ] 1.2 增加一次性硬切换前置检查，能够列出 Celery active、reserved、scheduled 及待迁移任务，并在任一硬门槛失败时阻止切换
- [x] 1.3 明确生产版本不包含 `execution_mode`、Celery fallback、双发保护或回切适配器；旧 Celery 入口只在硬切换前的历史代码中存在
- [x] 1.4 补充内部任务模块边界文档，明确 Domain Scheduler、Task Scheduler 和 Task executor 在同一 `scheduler-worker` 中的独立职责与健康状态

## 2. 数据库控制层

- [x] 2.1 新增数据库迁移和模型：`qd_task_definitions`、`qd_task_schedules`、`qd_task_runs`、`qd_task_events`、`qd_task_leases`、`qd_task_audit`
- [x] 2.2 为 `schedule_id + scheduled_at`、活动 `exclusivity_key`、Run 状态、优先级 FIFO、事件时间和领域引用建立唯一约束及查询索引
- [x] 2.3 实现通用 repository，覆盖计划修订、Run 创建、合法状态转换、结果摘要、领域引用、retry 关联和不可变终态
- [x] 2.4 为数据库约束、并发创建、非法状态转换、历史保留和领域关联添加针对性测试

## 3. Task Definition 注册表

- [x] 3.1 实现代码 Task Registry，声明稳定 `task_key`、参数 Schema、默认值、版本、优先级、互斥策略、retry/timeout/cancel 能力和领域适配器
- [ ] 3.2 实现 worker 启动时注册表幂等同步、新定义物化、移除定义 `retired` 和不一致时阻止 Task Scheduler 启动
- [ ] 3.3 实现 Run 创建时 definition/Schema/参数快照固化，以及缺失旧处理器时 `definition_version_unavailable` 失败路径
- [ ] 3.4 添加注册表 Schema 校验、版本升级、retired 任务、非法 Shell/Python/SQL 入口和旧版本不可用测试

## 4. Cron 调度与领取顺序

- [ ] 4.1 实现标准五字段 Cron、计划时区、默认 `Asia/Shanghai`、5 秒扫描及下一次未来触发计算
- [ ] 4.2 使用 PostgreSQL advisory lock 和唯一约束实现多实例分钟级去重，并记录 `missed_skipped` 和 `overlap_skipped`
- [ ] 4.3 实现注册代码生成的 `exclusivity_key`，使手工重复启动返回现有 Run、Cron 重叠不创建 Run
- [ ] 4.4 实现 critical/high/normal/low 固定优先级、优先级内 FIFO、全局并发 1，并预留 Task Definition 级并发字段
- [ ] 4.5 添加时区、分钟边界、停机跳过、多实例竞争、互斥范围、重复手工启动和优先级 FIFO 测试

## 5. Lease 与隔离执行器

- [ ] 5.1 实现 Run lease 获取、60 秒有效期、父进程每 20 秒续租以及只有 lease 持有者可提交状态和事件的校验
- [ ] 5.2 实现 180 秒无有效心跳后的 `failed/worker_lost` 协调逻辑，保留最后心跳、lease holder 和检查点引用且不自动续跑
- [ ] 5.3 实现每个 Run 独立子进程及受控 IPC，父进程负责状态、进度、事件、lease 和最终结果落库
- [ ] 5.4 实现 `cancel_requested` 协作信号、Task Definition 级取消宽限期、默认 60 秒和 `failed/forced_interruption` 强制终止路径
- [ ] 5.5 添加父进程、子进程异常退出、lease 竞争、容器重启、协作取消和取消超时的故障注入测试

## 6. Retry、超时、进度与事件

- [ ] 6.1 建立临时/永久错误分类契约，实现同一 Run 内指数退避加 jitter 的自动 attempt 和 `retry_wait` 状态
- [ ] 6.2 实现管理员人工 retry 新建 Run、`retry_of_run_id` 关联、原终态不变及 checkpoint 选择校验
- [ ] 6.3 实现 Task Definition 级总时长、provider 请求和取消超时策略，并支持基本面回填无总墙钟限制
- [ ] 6.4 实现最新进度快照、5 秒普通进度限频、关键事件即时写入，以及“领域检查点成功后才推进进度”的适配契约
- [ ] 6.5 实现结构化 Task Event、metadata 大小上限、`metadata_truncated`、敏感字段脱敏和 External Data Request Log 关联
- [ ] 6.6 实现 Task Event 默认 90 天批量清理，确保最终 Run 摘要、结果引用和领域详情不被清除
- [ ] 6.7 添加 retry、永久错误、timeout、未知总量进度、检查点失败、日志脱敏、限长和保留清理测试

## 7. 后端管理 API 与权限

- [ ] 7.1 新增 Overview、Schedules、Runs、Logs 管理查询 API，提供稳定分页、排序、筛选、事件 cursor 和 Run 详情聚合
- [ ] 7.2 新增计划创建/修改/暂停接口，仅允许修改未来触发、注册 Schema 允许的参数并持久化修订审计
- [ ] 7.3 新增手工启动、取消和人工 retry 接口，处理互斥键重复返回、取消能力和 retry checkpoint 校验
- [ ] 7.4 实现管理员管理全部任务、普通用户只读查看本人用户任务的后端授权，并对越权对象采用不泄露存在性的响应
- [ ] 7.5 为每项管理操作记录 actor、时间、原因、目标和脱敏前后摘要，并添加权限、审计、筛选和 cursor API 测试

## 8. Web Task Management 页面

- [ ] 8.1 在 `/home/quantadinger/QuantDinger-Vue` 新增 Task Management 路由、权限菜单、API 客户端和国际化文案
- [ ] 8.2 实现 Overview 标签页，展示队列、运行、失败、失联、计划和最近事件摘要
- [ ] 8.3 实现 Schedules 标签页，支持注册任务下拉选择、Cron/时区/Schema 参数编辑、启用/暂停和修订信息
- [ ] 8.4 实现 Runs 标签页及详情抽屉/页面，展示进度、heartbeat、领域详情、结果摘要、错误码、取消和 retry 操作
- [ ] 8.5 实现 Logs 标签页，支持任务、状态、所有者、时间、错误码和关联 provider 请求筛选以及脱敏事件详情
- [ ] 8.6 实现活动数据每 5 秒轮询、页面离开时停止轮询和稳定 cursor 消费；不增加 Celery 兼容提示或 fallback 页面
- [ ] 8.7 添加权限、表单校验、重复启动、计划暂停、取消、retry、日志筛选和轮询生命周期的前端针对性测试

## 9. 第一阶段：维护与市场数据任务迁移

- [ ] 9.1 注册并迁移 worker heartbeat、runtime metadata cleanup、External Data Request Log cleanup 和 reflection 等维护任务
- [ ] 9.2 注册并迁移 market catalog sync、CN quote refresh、CN market-history targeted/daily sync，并连接现有领域批次和进度
- [ ] 9.3 注册并迁移 fundamental incremental sync 和 fundamental backfill，移除运行中 pause/resume，接入安全取消与新 Run retry
- [ ] 9.4 为基本面全市场回填配置无总墙钟限制、provider 请求超时、Eastmoney 限流、标的级检查点和全局互斥键
- [ ] 9.5 更新相关管理路由，移除第一阶段任务的 `send_task` 直接投递并改为内部 Task Run 创建
- [ ] 9.6 使用小批量市场数据和基本面目标验证调度、进度、provider 错误详情、取消、retry、部署中断和磁盘阈值行为

## 10. 第二阶段：AI 与 Agent 任务迁移

- [ ] 10.1 注册并迁移 reflection、AI calibration、fast AI analysis 和其他有限 AI 后台任务，配置任务级 timeout、priority 和用户互斥键
- [ ] 10.2 将 `app/utils/agent_jobs.py` 与 backtest Job 执行连接到 Task Run，同时保持现有 `/api/agent/v1/jobs` 查询和 SSE 外部契约
- [ ] 10.3 实现普通用户只能查看本人 Agent/AI Task Run，管理员可查看全局 Run 和脱敏事件
- [ ] 10.4 移除第二阶段任务的 Celery 投递路径，并添加多用户排队、用户域互斥、结果引用和 Agent SSE 回归测试

## 11. 最终切换与 Celery 退役

- [ ] 11.1 实现最终切换命令/流程：停止 Beat 和新投递、撤回未开始工作并记录 `migration_skipped`、终止活动工作并记录 `migration_interrupted`
- [ ] 11.2 确保迁移中断任务不自动创建内部 Run，管理员只能检查最后 checkpoint 后人工 retry，并验证审计链路
- [ ] 11.3 实现可自动执行的硬切换门槛检查，覆盖全部任务适配完成、Celery 队列为空、调度、互斥、retry、取消、worker loss、权限、日志、前端和硬切换演练
- [ ] 11.4 记录删除前 Git 提交、镜像版本和数据库恢复说明；任一硬门槛失败时阻止继续
- [ ] 11.5 全部门槛通过后从 Compose 和 GHCR Compose 删除 Celery Worker、Celery Beat 与 `redis-jobs`，保留缓存 Redis
- [ ] 11.6 删除 Celery app、task decorators、Beat 配置、send_task 适配器、Celery-only Python 依赖、环境变量、health/readiness 和告警
- [ ] 11.7 删除所有 Celery 兼容文档和配置说明，更新部署、进程角色、并发模型、模块边界、API 和运维文档，记录取消/retry、worker loss 和硬切换中断处理手册

## 12. 最终验证与低资源部署

- [ ] 12.1 运行 OpenSpec 严格验证、Python compile、相关后端 targeted tests、前端 targeted tests 和两份 Compose 配置校验
- [ ] 12.2 在构建前检查 RAM、swap 和磁盘，按单作业、顺序方式分别构建并部署 backend 和 frontend，禁止并发构建
- [ ] 12.3 在部署环境执行 Cron、手工启动、互斥、retry、安全取消、强制终止、worker restart、权限和90天清理验收
- [ ] 12.4 确认运行容器不再包含 Celery Worker、Celery Beat 或 `redis-jobs`，Task Management 页面和现有 Domain Scheduler 均健康
