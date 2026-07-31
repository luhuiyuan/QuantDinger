## ADDED Requirements

### Requirement: 代码注册且版本化的任务定义
系统 MUST 仅执行代码注册的 Task Definition；每个定义必须包含稳定 `task_key`、参数 Schema、版本、默认值、执行能力和互斥键策略。创建 Task Run 时必须固化定义版本和经校验的参数快照，且不得在部署后静默改用新版本。

#### Scenario: 旧版本处理器已经不可用
- **WHEN** 排队 Run 固化的 `definition_version` 在当前部署中不存在
- **THEN** 系统将 Run 标记为 `failed/definition_version_unavailable`，而不是使用最新版本执行

#### Scenario: 提交任意执行入口
- **WHEN** 客户端尝试提交 Shell、任意 Python 入口、SQL 或不符合 Schema 的参数
- **THEN** 系统拒绝请求且不创建 Task Run

### Requirement: 注册表启动同步与退役
`scheduler-worker` MUST 在启动 Task Scheduler 前幂等同步代码注册表；代码新增定义必须被物化，代码移除定义必须标记为 `retired` 并保留历史。注册同步不一致时 Task Scheduler MUST 不开始触发计划。

#### Scenario: 已注册任务从代码移除
- **WHEN** worker 启动时数据库存在但代码注册表不再包含某任务定义
- **THEN** 系统将定义标记为 `retired`、禁止新计划和新 Run，并保持历史可查询

### Requirement: 通用控制层与领域运行表并存
系统 MUST 使用通用定义、计划、Run、事件、租约和审计记录控制任务，并通过 `domain_kind` 与 `domain_run_id` 引用领域运行表。业务检查点和详细结果 MUST 由领域表保存，通用 Run 只保存受限摘要和引用。

#### Scenario: 基本面回填更新进度
- **WHEN** 回填完成一个标的并成功提交领域检查点
- **THEN** 系统更新通用 Run 进度并保留领域运行记录引用，不复制完整目标明细到通用表

### Requirement: 持久化 Task Run 状态机
Task Run MUST 只使用 `queued`、`running`、`retry_wait`、`cancel_requested`、`succeeded`、`failed`、`cancelled` 状态，并拒绝非法转换。安全取消 MUST 结束为 `cancelled`；worker 丢失、超时和强制中断 MUST 结束为 `failed` 并使用明确错误码。

#### Scenario: 安全取消完成
- **WHEN** 运行任务收到取消请求并在安全检查点正常退出
- **THEN** Run 从 `cancel_requested` 转为 `cancelled` 并记录最后检查点

#### Scenario: 子进程被强制终止
- **WHEN** 子进程超过取消宽限期且被父进程终止
- **THEN** Run 转为 `failed` 且错误码为 `forced_interruption`

### Requirement: Cron 调度、时区与漏触发语义
Task Scheduler MUST 每 5 秒扫描已启用计划，以标准五字段 Cron 和计划持久化时区计算触发，默认时区为 `Asia/Shanghai`。系统 MUST 以 `schedule_id + scheduled_at` 去重同一分钟触发，并跳过停机期间错过的执行而不补跑。

#### Scenario: 服务停机跨过多个计划时间
- **WHEN** Task Scheduler 恢复时发现计划在停机期间错过多个 Cron 时刻
- **THEN** 系统不创建补偿 Run，记录 `missed_skipped`，并只计算下一次未来触发

#### Scenario: 多实例同时扫描
- **WHEN** 两个 worker 在同一时间扫描相同计划
- **THEN** PostgreSQL advisory lock 和唯一约束保证最多创建一个 Run

### Requirement: 互斥键与重复启动
每个注册任务 MUST 由代码根据参数和领域上下文生成 `exclusivity_key`，且每个互斥键最多存在一个活动 Run。Cron 重叠 MUST 记录 `overlap_skipped` 且不创建 Run；手工重复启动 MUST 返回现有活动 Run。

#### Scenario: 回填仍在运行时 Cron 再次触发
- **WHEN** 相同互斥键已有 `running` Run 且计划到期
- **THEN** 系统不创建新 Run并记录 `overlap_skipped`

#### Scenario: 不同用户提交同类 Agent Job
- **WHEN** 两个用户提交能生成不同用户域互斥键的同类任务
- **THEN** 系统允许两个 Run 进入队列并由全局并发限制决定执行顺序

### Requirement: 固定优先级、FIFO 与并发限制
执行器 MUST 使用固定优先级和同优先级内 FIFO 领取 Run。第一阶段全局执行并发 MUST 为 1，数据模型 MUST 预留 Task Definition 级并发限制；客户端不得自行提升注册任务优先级。

#### Scenario: 不同优先级任务同时排队
- **WHEN** low Run 先入队而 critical Run 后入队且执行槽位释放
- **THEN** 执行器先领取 critical Run，再按各优先级内创建顺序处理

### Requirement: Run lease、心跳与 worker 丢失
父进程 MUST 为执行中的 Run 持有 60 秒 lease 并每 20 秒续租；系统在连续 180 秒无有效心跳后 MUST 将原 Run 标记为 `failed/worker_lost`，且不得自动恢复或自动创建新 Run。只有有效 lease 持有者可以提交状态和事件。

#### Scenario: scheduler-worker 容器异常退出
- **WHEN** Run 正在执行且 worker 在 180 秒内没有恢复有效心跳
- **THEN** 协调逻辑将原 Run 标记为 `failed/worker_lost`，管理员可从检查点人工创建重试 Run

### Requirement: 独立子进程与协作取消
每个 Task Run MUST 在独立子进程中执行，父进程 MUST 管理 lease、状态、事件和受控 IPC。运行中取消 MUST 先协作通知子进程；Task Definition MUST 可配置取消宽限期且默认 60 秒，超过宽限期后 MUST 强制终止。

#### Scenario: provider 调用结束后响应取消
- **WHEN** 子进程在 provider 请求期间收到取消并在请求边界到达安全检查点
- **THEN** 子进程提交检查点并正常退出，Run 标记为 `cancelled`

### Requirement: 自动 retry 与人工 retry
系统 MUST 对注册定义分类的临时错误在同一 Run 内按指数退避和 jitter 自动 retry，并进入 `retry_wait`；永久错误 MUST 立即失败。管理员人工 retry MUST 创建带 `retry_of_run_id` 的新 Run，不得修改原 Run 终态。

#### Scenario: 临时限流错误
- **WHEN** provider 返回被分类为临时限流的错误且未超过定义重试上限
- **THEN** Run 进入 `retry_wait`，记录独立 attempt 事件，并在退避到期后重新排队

#### Scenario: 管理员重试永久失败
- **WHEN** 管理员对永久失败 Run 发起 retry
- **THEN** 系统创建关联原 Run 的新 Run并保留原失败记录

### Requirement: 任务级超时
系统 MUST 以 Task Definition 管理总时长、provider 请求和取消宽限策略，不得使用统一 Celery 一小时限制。基本面全市场回填 MUST 允许无总墙钟时长限制，但仍必须设置 provider 请求超时、心跳和检查点。

#### Scenario: 无总时限回填持续超过一小时
- **WHEN** 基本面回填持续运行超过一小时且心跳、请求超时与检查点均正常
- **THEN** 系统继续执行而不因通用总时长限制终止

### Requirement: 进度快照、事件和检查点一致性
系统 MUST 分离最新进度快照、结构化 Task Event 和领域检查点，并仅在领域检查点成功提交后推进通用进度。普通进度写入 MUST 最多每 5 秒一次，阶段变化、错误、取消和完成 MUST 立即记录；未知总量必须允许 `progress_total` 为空。

#### Scenario: 领域检查点提交失败
- **WHEN** 子进程处理完一个目标但领域检查点未成功落库
- **THEN** 系统不得推进通用 Run 的已完成数量，并记录可运维错误事件
