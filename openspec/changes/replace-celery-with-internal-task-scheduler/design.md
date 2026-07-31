## Context

QuantDinger 当前通过 Celery Worker、Celery Beat 和独立的 `redis-jobs` 执行有限后台任务，同时由现有 scheduler 进程运行投资组合监控、支付扫描和信号告警等长生命周期领域循环。两类工作负载语义不同，但当前文档和运维入口容易把它们混为“定时任务”。

基本面全市场回填曾触发 Celery 3300 秒 soft limit 和 3600 秒 hard limit，子进程被终止后，领域运行表仍保持 `running`，管理员只能从零散请求日志推断失败原因。当前部署仅有 2 个逻辑 CPU、约 3.5 GiB 内存，第一阶段应保持单实例、低并发和无新增容器，同时必须为未来多实例执行保留正确性边界。

本设计采用 `CONTEXT.md` 中的术语：Task Scheduler 只调度有限 Task Run；Domain Scheduler 继续拥有可续租的长生命周期领域循环。架构选择及放弃 Celery 的权衡记录于 `docs/adr/0001-internal-task-scheduler-replaces-celery.md`。

## Goals / Non-Goals

**Goals:**

- 在现有 `scheduler-worker` 容器内提供持久化、可观测、可审计的有限任务调度与执行。
- 让计划、Run、进度、检查点、事件、取消、重试和 worker 丢失具有明确且可测试的语义。
- 为管理员提供统一 Web 管理页面；普通用户只查看自己提交的用户任务。
- 分两阶段迁移全部 Celery 任务，并在硬门槛通过后立即删除 Celery 运行组件和依赖。
- 第一阶段全局执行并发为 1，同时使模型可表达任务级并发和多 worker 租约。

**Non-Goals:**

- 不把 Domain Scheduler 的投资组合、支付和信号长循环转换为有限 Task Run。
- 不支持任意 Shell、任意 Python 入口、任意 SQL 或前端定义任务 Schema。
- 不支持运行中暂停/恢复；仅支持暂停未来计划、安全取消和创建新 Run 重试。
- 第一版不实现 SSE、跨主机高吞吐队列或自动从检查点恢复 worker 丢失的 Run。
- 不新增维护 worker、消息代理或其他 Docker 容器。

## Decisions

### 1. 复用 scheduler-worker，但分离领域循环与有限任务模块

`scheduler-worker` 同时托管 Domain Scheduler、Task Scheduler 和 Task executor，但三者使用独立模块、生命周期和健康状态。有限任务故障不得停止领域循环；领域循环 ownership 不复用 Task Run 状态。选择同容器是为了降低当前单机资源占用，未来可在不改变数据库契约的前提下拆分进程角色。

### 2. 通用控制层与领域运行表并存

新增 `qd_task_definitions`、`qd_task_schedules`、`qd_task_runs`、`qd_task_events`、`qd_task_leases` 和 `qd_task_audit`。`qd_task_runs` 通过 `domain_kind`、`domain_run_id` 指向 `qd_cn_fundamental_sync_runs/targets`、`qd_cn_history_sync_runs/targets`、`qd_agent_jobs` 等领域表。通用层拥有调度、生命周期、权限和运维摘要；领域表拥有业务检查点和详细结果，避免将所有领域数据塞入通用 JSON。

### 3. 代码注册表是 Task Definition 的真相来源

任务注册项声明稳定 `task_key`、参数 Schema、默认值、版本、执行入口、优先级、重试/超时/取消能力、互斥键生成器、进度单位和领域适配器。启动时幂等物化到数据库；代码移除的任务标记 `retired` 而不删除历史。Run 创建时固化 `definition_version`、Schema 版本和参数快照。旧版处理器不存在时明确失败为 `definition_version_unavailable`，不得自动改用新版语义。

### 4. 状态机区分自动 attempt 与人工 retry Run

Run 状态为 `queued`、`running`、`retry_wait`、`cancel_requested`、`succeeded`、`failed`、`cancelled`。临时错误的自动 retry 是同一 Run 内的新 attempt；管理员人工 retry 创建带 `retry_of_run_id` 的新 Run。`worker_lost`、`timed_out`、`forced_interruption` 和 `migration_interrupted` 是失败错误码，不扩展顶层状态。安全取消完成为 `cancelled`，异常中断为 `failed`。

### 5. PostgreSQL 同时承担调度幂等和执行租约

Task Scheduler 每 5 秒尝试获得 PostgreSQL advisory lock 并扫描已启用计划。Cron 使用标准五字段和计划自身时区，默认 `Asia/Shanghai`；`schedule_id + scheduled_at` 唯一约束保证分钟级去重。停机期间错过的执行全部跳过并记录 `missed_skipped`。Cron 遇到同互斥键活动 Run 时记录 `overlap_skipped`；手工重复启动返回现有 Run。

执行器以固定优先级和优先级内 FIFO 领取任务。第一阶段全局并发为 1；表结构保留任务级并发限制。优先级从高到低为 critical、high、normal、low。Run lease 为 60 秒，父进程每 20 秒续租；连续 180 秒无有效心跳后原 Run 失败为 `worker_lost`，不自动恢复。

### 6. 每个 Run 使用独立子进程

父进程拥有 lease、状态转换、事件落库和取消控制；子进程通过受控 IPC 报告结构化进度、检查点确认和结果，不直接修改通用控制表。取消先设置 `cancel_requested` 并发送协作信号；任务在安全检查点退出后成为 `cancelled`。`cancel_grace_seconds` 由 Task Definition 配置，默认 60 秒；超时后强制终止并失败为 `forced_interruption`。

### 7. 任务级超时和错误分类替代 Celery 全局超时

每个 Task Definition 声明总时长、provider 请求和取消宽限策略。基本面全市场回填不设置总墙钟时长限制，但仍要求 provider 请求超时、父进程心跳和领域检查点。临时错误按指数退避和 jitter 自动 retry；永久错误立即失败，只能由管理员人工创建新 Run。worker 丢失不自动恢复，避免在未知副作用后重复执行。

### 8. 最新快照、事件与领域检查点分离

`qd_task_runs` 保存 stage、current、total、unit、message、heartbeat 和受限结果摘要；`qd_task_events` 保存脱敏结构化事件；领域表保存可恢复检查点和详细结果。只有领域检查点提交成功后才能推进通用进度。普通进度最多每 5 秒写一次，阶段变化、错误、取消和完成立即写入。事件保留 90 天，Run 最终摘要和领域结果不随事件清理删除。

### 9. 管理 API 和统一前端页面

管理页面包含 Overview、Schedules、Runs、Logs 四个标签页，Run 详情通过抽屉或详情页展示。管理员可管理全部注册任务、计划和 Run；普通用户只读查看自己提交的用户任务。第一版每 5 秒轮询，列表和事件 API 使用稳定分页及 cursor 字段，为后续 SSE 保留升级空间。所有计划修改、启动、取消、重试、执行模式切换和迁移操作写入管理审计。

### 10. 单一执行归属和不可自动续跑的迁移

迁移期间每个 Task Definition 的 `execution_mode` 只能为 `celery`、`internal` 或 `disabled`，禁止双发。切换时先停止 Celery Beat 和新投递，撤回未执行项并记录 `migration_skipped`，立即终止活动 Celery 任务并记录 `failed/migration_interrupted`。系统不自动创建内部 Run；管理员检查领域检查点后人工重试。全部任务切换并通过硬门槛后，立即删除 Celery Worker、Celery Beat、`redis-jobs`、Celery 配置、健康检查和 Python 依赖。

## Risks / Trade-offs

- [自研任务平台重建了 Celery 的部分可靠性能力] → 以数据库唯一约束、advisory lock、lease、严格状态机、故障注入测试和删除硬门槛降低风险。
- [同容器的 Task executor 可能影响 Domain Scheduler] → Run 使用独立子进程、第一阶段全局并发 1，并分别暴露健康状态；后续可按同一数据库契约拆分部署。
- [父进程失联后 180 秒内 Run 仍显示运行] → UI 显示最后心跳和租约信息；180 秒是在误判与故障发现速度之间选择的宽松窗口。
- [立即终止 Celery 任务会丢失未提交工作] → 迁移前记录任务、检查点和操作者，统一标记 `migration_interrupted`，只允许人工确认后新建 Run。
- [删除 Celery 后软件回滚成本较高] → 删除前记录 Git 提交和镜像版本、验证数据库回退说明，并把全部硬门槛设为阻断条件。
- [90 天事件量可能增长] → 普通进度写入限频、metadata 限长、按索引批量清理；最终 Run 摘要不依赖事件回放。

## Migration Plan

1. 建立通用表、注册表、状态机、调度锁、租约、子进程协议和只读管理 API，不改变现有 Celery 投递。
2. 增加统一前端页面及权限、审计、事件清理和故障演练能力。
3. 第一阶段迁移维护与市场数据任务；每个任务按 `execution_mode` 保证单一归属，并完成小批量验证。
4. 第二阶段迁移 AI/Agent Job 和 fast analysis；保持现有 Agent Job API/SSE 外部契约，由领域适配器连接 Task Run。
5. 停止 Celery 新投递，撤回未执行任务，立即终止活动任务并记录迁移失败；由管理员决定是否从检查点手工重试。
6. 运行全部删除硬门槛：Cron、互斥、FIFO、retry、取消、超时、worker loss、权限、审计、90 天清理、Compose、前端和迁移行为均通过。
7. 立即删除 Celery Worker、Celery Beat、`redis-jobs` 和 Celery-only 代码、配置、依赖、health/readiness；顺序构建和部署受影响服务。

回滚只影响未来触发，不转移活动 Run。删除前可按 Task Definition 把未来执行切回 Celery；删除后只能恢复已记录的代码/镜像版本和兼容数据库结构。任何回滚都必须先暂停计划并处理活动内部 Run。

## Open Questions

无。实现阶段若发现现有领域表无法表达已确认的检查点或结果引用，应新增领域迁移，不得把未建模的业务详情转存为无限制 Task Run JSON。
