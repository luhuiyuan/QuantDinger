## ADDED Requirements

### Requirement: 分阶段迁移全部有限任务
系统 MUST 分阶段迁移全部 Celery 有限任务：第一阶段迁移维护和市场数据任务，第二阶段迁移 AI/Agent Job。Domain Scheduler 的长生命周期领域循环 MUST 保持独立且不得被误迁移为有限 Task Run。

#### Scenario: 第一阶段完成
- **WHEN** 维护、目录同步、行情同步和基本面同步任务完成内部执行验证
- **THEN** 这些任务不再通过 Celery 投递，而 AI/Agent 任务可继续按单一归属留在 Celery 直至第二阶段

### Requirement: 每个任务定义单一执行归属
迁移期间每个 Task Definition MUST 具有 `celery`、`internal` 或 `disabled` 的唯一执行模式；同一任务不得由 Celery 和内部执行器双发。模式切换 MUST 只影响未来创建的工作并记录管理员审计。

#### Scenario: 切换市场同步到内部执行
- **WHEN** 管理员将任务执行模式从 `celery` 切换为 `internal`
- **THEN** 后续触发只创建内部 Run，系统不得向 Celery 同时投递

### Requirement: 切换时立即终止 Celery 工作
最终切换时系统 MUST 停止 Celery Beat 和新投递，撤回未开始的 Celery 工作并记录 `migration_skipped`，立即终止活动 Celery 工作并记录 `failed/migration_interrupted`。系统 MUST 不自动创建内部 Run或自动从检查点续跑。

#### Scenario: 活动基本面回填在切换时被终止
- **WHEN** 最终切换发生且 Celery 正在执行基本面回填
- **THEN** 原任务记录为 `failed/migration_interrupted`，管理员检查最后领域检查点后才能手工创建内部 retry Run

### Requirement: 删除前硬验证门槛
系统和部署变更 MUST 在删除任何 Celery 运行组件前证明所有注册任务为 internal、Celery active/reserved/scheduled 均为空，并通过 Cron、跳过、互斥、FIFO、自动 retry、人工 retry、安全取消、强制终止、worker loss、进度、事件、审计、权限、90 天清理、前端和数据库回退验证。任一门槛失败 MUST 阻止删除。

#### Scenario: worker loss 故障演练失败
- **WHEN** 其他迁移测试通过但 worker loss 未能在 180 秒后正确标记失败
- **THEN** 发布流程阻止删除 Celery 组件并报告未满足门槛

### Requirement: 全部门槛通过后立即删除 Celery
全部硬门槛通过后，部署 MUST 立即删除 Celery Worker、Celery Beat、`redis-jobs`、Celery-only Python 依赖、配置、投递适配器、健康检查和告警。仍承担缓存职责的 Redis MUST 保留；删除前 MUST 记录 Git 提交、镜像版本和数据库恢复说明。

#### Scenario: 完成最终退役
- **WHEN** 所有任务已切换且硬门槛全部通过
- **THEN** Compose 不再启动 Celery Worker、Celery Beat 或 `redis-jobs`，后端 readiness 不再依赖它们，内部任务功能保持可用

### Requirement: 回滚不得转移活动 Run
迁移或软件回滚 MUST 只改变未来触发，并在切换前暂停计划和处理活动 Run。系统 MUST 不把活动内部 Run 转交 Celery，也不得把已终止 Celery 工作伪装为内部 Run。

#### Scenario: 内部执行切换后请求回滚
- **WHEN** 管理员在仍有活动内部 Run 时请求恢复旧版本
- **THEN** 运维流程先暂停未来计划并要求活动 Run完成、取消或失败，不转移执行所有权
