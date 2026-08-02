## Why

QuantDinger 的行情、基本面、宏观、新闻和目录等外部数据调用目前分散在各功能模块中，供应商选择、凭证、限流、缓存、质量校验、降级和健康状态缺少统一契约，管理员也无法从一次用户请求追溯完整的主源与 fallback 决策。项目需要在保持现有业务 API 兼容的前提下，把全部外部数据获取收口到代码注册、可审计且可运维的统一数据路由体系。

## What Changes

- 新增代码注册、显式版本化的 `Provider Adapter` 与 `Data Capability` 注册表；管理员只能配置已注册 Adapter 的 `Provider Instance`，不得创建动态插件、任意端点或可执行路由逻辑。
- 新增 Provider Instance 生命周期、数据库加密凭证、只写轮换、账户唯一性、能力验证、配额、安全预算、健康、能力级/实例级熔断、隔离和专用恢复探测。
- 新增按 Data Capability 管理的版本化 `Data Routing Policy`，采用草稿、完整校验、显式发布、不可变历史和从旧版本创建新修订的恢复方式；请求在开始时固定策略版本并按不可放宽的 `Data Eligibility Constraint` 过滤有序实例。
- 新增统一 `Routed Data Request`、`Provider Attempt`、`Routing Cache`、运行时质量门和来源说明；交互请求遵循新鲜缓存 → 有序 Provider → 合格陈旧缓存，后台任务按 Capability 数据流固定 Provider 并按安全检查点处理配额和失败。
- 新增管理员 Data Source Operations Console，包含 Overview、Provider Instances、Routing Policies 三个标签页，并链接独立的 Data Source Settings 与 External Data Request Logs；后端提供细分权限、Step-up、变更理由和失败关闭的管理审计。
- 迁移全部外部数据获取链路，包括市场、K 线、报价、指数、基本面、财报、公司行为、目录、宏观、经济日历、商品、新闻、情绪输入及通过 CCXT 等 SDK 获取的公开行情；排除交易账户、持仓、下单、LLM、通知、支付、OAuth 与其他非数据服务。
- 保持现有前端、策略、回测、Agent 与 MCP 业务 API 向后兼容，通过可选字段或响应头增量返回 Routed Data Request ID、来源、时效和质量警告。
- 提供管理员确认的一次性旧凭证安全导入、真实部署受控预检、零绕过调用清单和不可绕过的切换门槛。
- **BREAKING**：全部范围内链路验证完成后，在停止整个应用的维护窗口内一次性切换；目标版本删除旧功能内硬编码 provider/fallback 与旧凭证读取路径，不提供运行时双路由或旧版本回退，切换后只允许向前修复。

## Capabilities

### New Capabilities

- `external-data-provider-control-plane`: Provider Adapter、Data Capability、Provider Instance、加密凭证、账户唯一性、能力资格、配额和版本迁移。
- `routed-external-data`: Data Routing Policy、请求约束、Provider Attempt、Routing Cache、质量门、交互与后台路由、来源说明及 API 兼容性。
- `external-data-provider-health`: 实例级与能力级健康、熔断、隔离、恢复探测、诊断测试、最后有效控制面快照和降级行为。
- `data-source-operations-console`: Overview、Provider Instances、Routing Policies 管理 API/Web 页面，以及日志、设置、权限、审计和留存入口。
- `all-source-routing-cutover`: 全量调用盘点、旧凭证导入、真实环境预检、不可绕过门槛、全应用维护窗口、一次性前向切换和旧路径删除。

### Modified Capabilities

- `cn-market-data-operations`: A 股历史、基本面和公司行为等后台同步通过统一路由按 Capability 数据流固定 Provider，并把供应商健康与路由运维入口迁移到 Data Source Operations Console。

## Impact

- 后端：`backend_api_python/app/data_sources/`、`app/data_providers/`、市场与基本面同步服务、缓存、外部请求日志、设置、认证权限、readiness、Task Scheduler 维护任务、管理 API 和 OpenAPI。
- 数据库：新增 Adapter/Capability 物化、Provider Instance、加密凭证、账户身份、能力资格、配额、健康/熔断、路由修订、Routed Data Request、管理审计和切换状态相关表、约束与索引。
- Web 前端：`/home/quantadinger/QuantDinger-Vue` 新增 Data Source Operations Console、权限控制、实例与路由工作流、多语言文案，并调整现有设置和请求日志入口；Mobile/H5 不新增管理员页面。
- 部署与安全：新增独立凭证加密密钥及双密钥轮换、旧凭证导入、全应用维护切换流程、readiness 门槛和运维文档；不新增容器，继续遵守单机低资源、顺序构建与串行后台任务约束。
- 兼容性：现有业务 API 保持向后兼容；旧 provider/fallback 代码和旧凭证读取在硬切换目标版本中删除。
- 测试：新增注册表与版本迁移、凭证安全、账户去重、策略发布、约束过滤、缓存顺序、质量门、配额、熔断恢复、后台固定来源、权限审计、日志降级、全量调用清单、前端交互和不可回滚切换演练测试。
