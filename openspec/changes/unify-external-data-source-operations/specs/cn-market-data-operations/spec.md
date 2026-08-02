## ADDED Requirements

### Requirement: 统一路由下的后台来源一致性
A 股行情历史、基本面、公司行为和引用核验后台任务 MUST 为每个 Data Capability 数据流通过统一 Data Routing Policy 选择并固定一个 Provider Instance，并把策略修订、Instance、检查点和来源变化写入领域运行记录；一个任务的不同 Capability MAY 使用不同固定 Instance，但同一数据流不得静默混源。

#### Scenario: 日线与官方公司行为引用使用不同来源
- **WHEN** A 股历史同步需要日线数据和独立官方公司行为引用
- **THEN** 两个 Capability 数据流分别固定经验证 Instance并独立记录来源

#### Scenario: 日线 Provider 中途失败
- **WHEN** 日线数据流已提交部分分页后固定 Instance 失败
- **THEN** 任务从安全检查点使用同一 Instance 有界重试或延期且不在当前数据流静默切换

## MODIFIED Requirements

### Requirement: 供应商健康状态
系统必须（SHALL）在 Data Source Operations Console 中提供 A 股相关 Provider Instance 的整体健康、各 Data Capability 健康、最近探测、延迟、连续失败、熔断/退避、最近成功、配额和当前路由资格。easy_tdx Adapter 内部候选节点诊断必须作为该 Instance 的 Adapter 详情保留，但节点切换不得被建模为管理员可创建的 Provider Instance。健康检查不得在普通只读请求中产生无限或高并发探测。

#### Scenario: 所有 TDX 候选节点不可用
- **WHEN** easy_tdx Instance 的所有候选节点均处于失败或退避状态
- **THEN** 该 Instance 的 A 股历史 Capability 显示不可用且同步任务不得把目标覆盖标记为完成

#### Scenario: 其他 Capability 仍健康
- **WHEN** 同一 Provider Instance 的 A 股历史能力失败但独立报价能力仍通过健康证据
- **THEN** 控制台只熔断已证明失败的 Capability

### Requirement: 前端展示与兼容降级
Web 管理端必须（SHALL）通过 Data Source Operations Console 展示 Provider Instance、路由、健康、配额和诊断，并保留 A 股覆盖、质量、批次和磁盘保护的领域运维视图及受控定向同步操作；回测中心必须展示覆盖错误、数据来源和执行假设。前端连接旧后端或统一路由未 Ready 时必须降级为不可用提示，不得伪造健康、路由或完整状态。

#### Scenario: 新端点不可用
- **WHEN** 前端调用统一数据源运维端点得到明确未支持或 not-ready 响应
- **THEN** 界面显示后端尚未启用该能力且禁用相关管理操作

#### Scenario: 从 A 股覆盖问题进入路由调查
- **WHEN** 管理员从有缺口的 A 股批次查看来源问题
- **THEN** 界面可跳转到对应 Routed Data Request 或 Provider Instance 且不复制控制台功能
