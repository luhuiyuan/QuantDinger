## ADDED Requirements

### Requirement: 实例级与能力级健康
系统 MUST 同时维护 Provider Instance 整体健康和 Instance + Data Capability 健康。结构、语义、端点、能力质量及能力专属配额故障 MUST 默认只影响对应 Capability；认证、账户暂停、账户级总配额及已证明的 Provider 整体故障 MUST 影响整个 Instance。

#### Scenario: 单一 Capability Schema 变化
- **WHEN** 一个 Instance 的 K 线端点结构失效但报价端点仍通过验证
- **THEN** 系统只降低 K 线 Capability 健康且报价仍可路由

#### Scenario: 账户认证失效
- **WHEN** Provider 明确拒绝 Instance 凭证
- **THEN** 系统立即影响整个 Instance 的全部 Capability

### Requirement: 未知故障保守扩散
无法证明范围的新故障 MUST 先按 Instance + Capability 记录，只有跨 Capability 证据建立更广影响时才能升级为 Instance 故障。

#### Scenario: 首次未知异常
- **WHEN** 一个 Capability 返回未分类 Provider 错误且其他能力没有失败证据
- **THEN** 系统不得立即停用整个 Instance

### Requirement: 分类熔断开启
永久认证、账户、配置或明确能力不可用故障 MUST 在其已证明范围立即打开熔断；临时 transport、5xx、rate-limit 和质量故障 MUST 仅在达到代码声明的滚动阈值与最小样本量后打开熔断。

#### Scenario: 单次临时超时
- **WHEN** 健康 Instance 发生一次临时网络超时且未达到阈值
- **THEN** 系统记录失败但不立即打开熔断

#### Scenario: 凭证被明确撤销
- **WHEN** Provider 返回永久认证失败
- **THEN** Instance 级熔断立即打开并阻止新 Attempt

### Requirement: 专用恢复探测
临时故障熔断 MUST 在有界 cooldown 后使用低频专用恢复探测，不得使用普通用户流量充当 half-open 探测；成功证据满足阈值后才能关闭，失败 MUST 重新打开并有界退避。管理员 SHALL 可提前触发同一受控探测。

#### Scenario: cooldown 后探测成功
- **WHEN** 受影响 Capability 的恢复探测满足代码定义的成功证据
- **THEN** 系统关闭对应熔断并恢复新请求资格

#### Scenario: 永久凭证故障未修复
- **WHEN** 熔断原因是凭证无效且凭证未变化
- **THEN** 系统不自动重复消耗 Provider 请求进行恢复探测

### Requirement: 管理员不得伪造健康
管理员 SHALL 可手工隔离 Instance/Capability、延长熔断和触发恢复探测，但 MUST 不得直接标记健康或强制生产流量绕过熔断。

#### Scenario: 管理员尝试强制关闭熔断
- **WHEN** 恢复证据尚未满足且管理员请求标记健康
- **THEN** 后端拒绝操作并提示使用恢复探测或调整路由

### Requirement: Provider Capability Test
管理员触发的 Provider Capability Test MUST 绕过路由和 fallback，但继续使用正常凭证、配额、标准化、质量门、脱敏和日志；测试结果 MUST 单独记录且不得直接改变生产熔断状态。

#### Scenario: 诊断测试成功但生产熔断仍开
- **WHEN** 管理员执行一次成功 Capability Test
- **THEN** 系统展示诊断结果但保持熔断，除非随后受控恢复探测满足恢复证据

### Requirement: 禁用对进行中工作的影响
Instance 禁用或隔离 MUST 立即阻止新请求和未开始 Attempt；已发出的调用 MAY 完成并记录。固定该 Instance 的后台 Capability 数据流 MUST 在当前安全分块完成后停止，不得启动下一分块或静默切源。

#### Scenario: 批处理分块期间隔离 Instance
- **WHEN** 管理员在已发出请求的安全分块执行中隔离固定 Instance
- **THEN** 当前分块可完成，但后续分块不得开始

### Requirement: 最后有效控制面快照
配置数据库暂时不可用时，已有进程 SHALL 可在有界窗口内使用最后一次验证通过的内存策略、Instance 配置和凭证继续路由，同时冻结管理变更并报告 stale control plane；无有效快照的新进程 MUST 不得进入 Ready。

#### Scenario: 运行中数据库短暂中断
- **WHEN** 已 Ready 进程失去配置数据库连接但仍在降级窗口内
- **THEN** 它使用最后有效快照继续路由并暴露控制面降级状态

#### Scenario: 无数据库时启动新进程
- **WHEN** 新进程无法读取有效控制面配置
- **THEN** readiness 失败且不读取旧环境配置作为替代路由
