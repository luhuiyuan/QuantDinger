## ADDED Requirements

### Requirement: 分阶段迁移全部有限任务
系统 MUST 分阶段完成全部有限任务的内部适配：第一阶段完成维护和市场数据任务，第二阶段完成 AI/Agent Job；分阶段只表示开发和验证顺序，不表示生产运行期保留双后端。Domain Scheduler 的长生命周期领域循环 MUST 保持独立且不得被误迁移为有限 Task Run。

#### Scenario: 第一阶段完成
- **WHEN** 维护、目录同步、行情同步和基本面同步任务完成内部执行验证
- **THEN** 这些任务已具备内部 Task Run 适配和验收证据，但在最终硬切换前不发布部分生产迁移状态

### Requirement: 生产版本不得包含 Celery 兼容层
最终生产版本 MUST 不包含 `execution_mode`、Celery/内部双后端、fallback、运行时切换或回切适配器；所有有限任务入口 MUST 直接创建内部 Task Run，Domain Scheduler 仍只负责长生命周期领域循环。

#### Scenario: 检查最终生产制品
- **WHEN** 发布流程扫描代码、配置和 Compose 定义
- **THEN** 不存在 Celery 投递调用、兼容模式字段、fallback 分支或 Celery 容器定义

### Requirement: 切换时立即终止 Celery 工作
最终切换时系统 MUST 停止 Celery Beat 和新投递，撤回未开始的 Celery 工作并记录 `migration_skipped`，立即终止活动 Celery 工作并记录 `failed/migration_interrupted`。系统 MUST 不自动创建内部 Run或自动从检查点续跑。

#### Scenario: 活动基本面回填在切换时被终止
- **WHEN** 最终切换发生且 Celery 正在执行基本面回填
- **THEN** 原任务记录为 `failed/migration_interrupted`，管理员检查最后领域检查点后才能手工创建内部 retry Run

### Requirement: 删除前硬验证门槛
系统和部署变更 MUST 在硬切换前证明全部有限任务适配已完成、Celery active/reserved/scheduled 均为空，并通过 Cron、跳过、互斥、FIFO、自动 retry、人工 retry、安全取消、强制终止、worker loss、进度、事件、审计、权限、90 天清理、前端和硬切换演练验证。任一门槛失败 MUST 阻止硬切换。

#### Scenario: worker loss 故障演练失败
- **WHEN** 其他迁移测试通过但 worker loss 未能在 180 秒后正确标记失败
- **THEN** 发布流程阻止删除 Celery 组件并报告未满足门槛

### Requirement: 全部门槛通过后立即删除 Celery
全部硬门槛通过后，部署 MUST 立即删除 Celery Worker、Celery Beat、`redis-jobs`、Celery-only Python 依赖、配置、投递适配器、健康检查和告警。仍承担缓存职责的 Redis MUST 保留；删除前 MUST 记录 Git 提交、镜像版本和数据库恢复说明。

#### Scenario: 完成最终退役
- **WHEN** 所有任务已切换且硬门槛全部通过
- **THEN** Compose 不再启动 Celery Worker、Celery Beat 或 `redis-jobs`，后端 readiness 不再依赖它们，内部任务功能保持可用

### Requirement: 硬切换失败不得运行时回切
硬切换失败 MUST 停止新计划并保留内部 Run 现场，修复后重新部署；系统 MUST 不把活动内部 Run 转交 Celery，也不得把已终止 Celery 工作伪装为内部 Run。软件版本回退只能恢复完整旧制品和部署编排。

#### Scenario: 硬切换后发现内部执行缺陷
- **WHEN** 管理员发现内部执行缺陷并请求恢复旧版本
- **THEN** 系统暂停未来计划并保留活动 Run，运维只能通过完整旧制品回退，不存在新版本到 Celery 的运行时切换
