## ADDED Requirements

### Requirement: 市场数据任务接入内部 Task Run
行情历史同步、行情刷新、市场目录同步、基本面增量同步和基本面历史回填 MUST 通过注册 Task Definition 创建内部 Task Run，并将通用生命周期、进度和事件关联到现有领域批次与目标表。

#### Scenario: 管理员启动基本面历史回填
- **WHEN** 管理员提交合法的全市场基本面回填参数
- **THEN** 系统创建领域运行记录和关联的内部 Task Run，并立即返回 Run 标识而不阻塞 HTTP 请求

## MODIFIED Requirements

### Requirement: 磁盘空间保护
系统必须（SHALL）在创建扩大覆盖的同步任务及每个写入批次前检查配置的软硬空间阈值。达到软阈值时拒绝新的扩容任务；达到硬阈值时必须请求当前 Task Run 在安全提交边界取消并持久化检查点，不得把 Run 保持为可暂停状态。系统不得自动删除项目数据、Docker 缓存或共享缓存。

#### Scenario: 启动前达到软阈值
- **WHEN** 可用磁盘空间低于软阈值但尚高于硬阈值
- **THEN** 新的回填任务被拒绝，状态接口返回阈值、实测空间和人工处理提示

#### Scenario: 执行中达到硬阈值
- **WHEN** 同步过程中可用空间降至硬阈值
- **THEN** 当前 Task Run 在最近安全提交边界保存检查点并结束为 `cancelled`，管理员释放空间后可创建新的 retry Run

### Requirement: 基本面历史受控回填
系统 MUST 仅允许管理员创建基本面年度历史回填，并按标的持久化状态、检查点、尝试次数和错误。运行中回填 MUST 支持检查点式安全取消但不得暂停或原地恢复；管理员 retry MUST 创建关联原 Run 的新 Task Run并从已确认检查点继续。回填 MUST 不设置总墙钟时长限制，但必须保留 provider 请求超时、心跳和检查点保护。

#### Scenario: 回填取消后重试
- **WHEN** 管理员安全取消部分完成的基本面回填并随后发起 retry
- **THEN** 原 Run 保持 `cancelled`，系统创建关联的新 Run并从最近成功检查点继续，不重复覆盖已保存版本

### Requirement: 基本面增量同步与资源保护
系统 MUST 每天 20:30 按 `Asia/Shanghai` 调度增量同步，且不得与相同互斥范围的回填重叠；任务在创建和写入时必须沿用磁盘阈值、串行处理、限流和运维审计保护。Cron 触发遇到活动回填时 MUST 跳过并记录 `overlap_skipped`，不得补跑或创建等待重叠的 Run。

#### Scenario: 增量任务遇到运行中回填
- **WHEN** 每日增量任务触发时存在相同互斥范围的活动回填
- **THEN** 系统不创建增量 Run并记录 `overlap_skipped`，且不并发采集
