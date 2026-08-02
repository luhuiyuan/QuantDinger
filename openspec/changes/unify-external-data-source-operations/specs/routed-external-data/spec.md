## ADDED Requirements

### Requirement: 全局版本化路由策略
每个 Data Capability MUST 具有一个部署级有序 Provider Instance 列表。管理员 MUST 在草稿修订中编辑，经完整后端校验和影响预览后显式发布；发布 MUST 原子替换生效修订并保留不可变历史。

#### Scenario: 发布有效草稿
- **WHEN** 草稿中的 Instance 均存在、已激活、验证支持 Capability 且满足约束
- **THEN** 系统原子发布新修订并只让新请求使用它

#### Scenario: 从旧版本恢复
- **WHEN** 管理员选择旧修订进行恢复
- **THEN** 系统创建新草稿、按当前 Instance 重新校验并发布为新的历史修订

### Requirement: 显式停用 Capability
系统 SHALL 允许管理员带理由显式停用 Data Capability；启用策略 MUST 至少包含一个有效 Instance，空有序列表 MUST 视为无效配置而不是隐式停用。

#### Scenario: 发布空路由
- **WHEN** 管理员尝试为启用 Capability 发布空列表
- **THEN** 后端拒绝发布并要求显式停用或添加有效 Instance

### Requirement: 约束过滤不得放宽
Routed Data Request MUST 携带代码允许的 Data Eligibility Constraints，路由 MUST 从固定策略顺序中过滤不满足市场、venue、复权、时效或回测资格等约束的 Instance，并且 MUST 不得为了得到结果而放宽约束或执行管理员自定义条件表达式。

#### Scenario: 首选 Provider 不支持请求复权语义
- **WHEN** 首选 Instance 无法满足请求的 adjustment constraint 而后续 Instance 可以满足
- **THEN** 系统跳过首选并按原策略顺序选择后续合格 Instance

#### Scenario: 所有 Instance 均不满足硬约束
- **WHEN** 策略列表过滤后没有合格 Instance 且无合格缓存
- **THEN** 请求结构化失败且不调用不合格 Provider

### Requirement: 请求固定策略修订
每个 Routed Data Request MUST 在创建时固化生效策略修订并在整条 fallback 链中保持顺序；后台任务 MUST 为每个 Capability 数据流记录启动时的修订和固定 Instance。新策略不得改变进行中工作，但当前禁用、隔离或熔断状态 MUST 阻止尚未开始的 Attempt。

#### Scenario: 请求执行期间发布新策略
- **WHEN** Routed Data Request 已完成主源失败且管理员发布不同 fallback 顺序
- **THEN** 该请求继续使用创建时固化的旧修订顺序

#### Scenario: 固化修订中的 Instance 被紧急隔离
- **WHEN** 尚未开始的 fallback Instance 在请求期间被隔离
- **THEN** Attempt 记录为跳过且不得向该 Instance 发出调用

### Requirement: Routing Cache 边界与交互顺序
Routing Cache MUST 只保存以前通过质量门的路由结果并包含决定语义的完整 Key、原 Provider 来源和获取时间；持久化历史/回测数据集及 Provider 侧缓存 MUST 不属于 Routing Cache。交互请求 MUST 按合格新鲜缓存、策略内 Provider、合格陈旧缓存的顺序执行。

#### Scenario: 命中新鲜缓存
- **WHEN** 新鲜 Routing Cache 满足请求全部约束
- **THEN** 系统直接返回缓存并创建零个 Provider Attempt

#### Scenario: Provider 全部失败后存在合格陈旧缓存
- **WHEN** 时间预算内无 Provider 成功且陈旧缓存仍满足请求允许的 fallback 窗口
- **THEN** 系统返回明确标记为 stale 的缓存结果

### Requirement: Capability 停用期间的缓存
Capability 显式停用 MUST 阻止新的 Provider Attempt，但 SHALL 允许返回仍满足当前 Data Request Mode 和全部约束的 Routing Cache；没有合格缓存时 MUST 返回稳定 `capability_disabled` 结果。

#### Scenario: 停用期间存在新鲜缓存
- **WHEN** Capability 已停用且请求命中满足约束的新鲜缓存
- **THEN** 系统返回缓存且不调用 Provider

### Requirement: Runtime Data Quality Gate
可信代码 MUST 定义不可关闭的通用与 Capability 硬质量门，Adapter MUST 先标准化结果且不得绕过门槛。管理员只可选择代码声明的更严格档位或提高有界阈值，不得降低硬门槛或上传校验代码。

#### Scenario: Provider 结果未通过硬门
- **WHEN** 标准化结果违反结构、时间或 Capability 语义硬门槛
- **THEN** 本次 Provider Attempt 被拒绝并允许继续 fallback

#### Scenario: 结果仅含非致命警告
- **WHEN** 结果通过全部硬门但产生结构化非致命警告
- **THEN** 系统接受当前结果、返回警告且不因警告额外调用下一 Provider

### Requirement: Routed Request 与 Provider Attempt
每次路由调用 MUST 创建稳定 Routed Data Request ID 和完整结果摘要，并为策略内主源、fallback、禁用、熔断、配额跳过及实际外部调用创建有序 Provider Attempt；有限传输重试 MUST 汇总在同一 Attempt 中而不得伪装成独立 Provider。

#### Scenario: 主源失败后 fallback 成功
- **WHEN** 主 Provider 返回临时错误且第二 Provider 返回合格结果
- **THEN** 两个 Attempt 共享同一 Routed Data Request ID、具有稳定顺序并解释最终选择

#### Scenario: 已知配额耗尽
- **WHEN** Instance 配额重置不在交互等待预算内
- **THEN** 系统不发出外部调用、记录 `quota_exhausted` skipped Attempt 并继续下一 Instance

### Requirement: 运维日志失败开放
用户和后台数据获取 MUST 在 Routed Data Request 或 Provider Attempt 运维日志写入失败时继续执行，并 MUST 发出可观测性降级指标与告警；安全和管理审计不适用此失败开放规则。

#### Scenario: Provider 成功但日志数据库暂时不可写
- **WHEN** Provider 返回合格数据而运维日志 INSERT 失败
- **THEN** 调用方仍收到数据且系统报告 degraded observability

### Requirement: 后台 Capability 数据流来源一致
后台任务 MUST 为每个 Capability 数据流在实质进度前固定一个合格 Provider Instance，并按领域安全检查点持久化来源。不同 Capability 可固定不同 Instance，但同一 Capability 的数据集/检查点范围 MUST 不得静默混合多个 Provider。

#### Scenario: 同步同时需要价格与公司行为引用
- **WHEN** 一个任务分别读取价格历史 Capability 和官方公司行为引用 Capability
- **THEN** 两个数据流可固定不同 Instance且分别记录来源

#### Scenario: 固定 Provider 在分块中途失败
- **WHEN** 后台数据流已提交部分分块后固定 Instance 暂时不可用
- **THEN** 任务使用同一 Instance 有界等待、重试或延期，并且切换来源必须从明确新 Attempt 或 Task Run 的安全检查点开始

### Requirement: 后台配额规划
Adapter MUST 保守估算后台 Capability 数据流下一个安全提交分块的配额成本；执行 MUST 在开始分块前验证或预留容量，配额不足时在任务预算内等待或延期，且不得故意开始预计会在提交前耗尽配额的分块。

#### Scenario: 下一分块额度不足
- **WHEN** 固定 Instance 的已知剩余额度低于下一个安全分块估算成本
- **THEN** 任务不开始该分块并等待重置或延期

### Requirement: 现有业务 API 向后兼容
统一路由 MUST 保持现有业务路由、请求参数、成功 payload 和既有领域错误语义兼容；Routed Data Request ID、公开 Provider 名称、获取时间、fresh/stale 状态和非敏感质量警告 MUST 仅通过可选字段或响应头增量提供。

#### Scenario: 旧客户端忽略新增来源字段
- **WHEN** 现有前端或 Agent 客户端按旧响应字段解析成功请求
- **THEN** 客户端继续工作且无需同步破坏性升级

### Requirement: 普通用户来源信息边界
普通用户 SHALL 只能在自己的响应中查看安全来源说明，不得浏览路由顺序、Provider Attempt、其他用户或后台请求、内部健康、配额、凭证、原始参数或错误详情。

#### Scenario: 普通用户请求运维日志列表
- **WHEN** 非管理员调用 Routed Request 或 Provider Attempt 管理查询
- **THEN** 后端拒绝访问且不泄漏是否存在目标记录
