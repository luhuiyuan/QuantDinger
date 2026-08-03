## ADDED Requirements

### Requirement: 全部外部数据获取纳入范围
第一版 MUST 迁移全部用于市场、K 线、报价、指数、基本面、财报、公司行为、目录、证券主数据、宏观、经济日历、商品、新闻、情绪输入和其他分析数据的数据调用，包括通过 CCXT 等 SDK 的公开行情调用；交易账户、持仓、下单、LLM、通知、支付、OAuth 和非数据基础设施调用 MUST 排除。

#### Scenario: 同一交易所同时提供行情与下单
- **WHEN** 一个 exchange SDK 同时用于公开 K 线和用户订单
- **THEN** K 线调用迁入统一数据路由而订单调用保持在交易边界之外

### Requirement: 零绕过调用清单
系统 MUST 维护并验证全部范围内 outbound data call inventory，目标版本 MUST 不存在绕过统一路由的功能内硬编码 provider/fallback、直接 HTTP、SDK 或第三方库数据调用。

#### Scenario: 静态检查发现未注册数据调用
- **WHEN** 切换检查发现某新闻功能直接调用外部搜索 Provider 而未经过注册 Adapter
- **THEN** 切换门槛失败且不得把该路径标记为可接受遗留

### Requirement: 一次性旧凭证安全导入
系统 MUST 提供管理员 Step-up 确认的一次性导入，将受支持环境变量和本地配置中的旧数据源凭证直接写入加密 Provider Instance 记录而不显示明文，并逐项验证。切换后 MUST 删除旧凭证读取路径并提示清除旧值。

#### Scenario: 必需凭证导入失败
- **WHEN** 一个启用 Capability 所需 Provider 凭证无法导入或验证
- **THEN** 生产切换被阻止直至重新导入、手工替换或显式停用该 Capability

### Requirement: 真实环境受控预检
生产用户仍走旧路径时，管理员 MUST 能在真实部署中对全部新 Adapter、Instance 和 Capability 执行低频受控预检及不提交正式结果的后台演练；系统 MUST 不把全部生产请求复制为影子流量。

#### Scenario: Provider 预检
- **WHEN** 管理员在切换前对已配置 Instance 执行 Capability 预检
- **THEN** 请求经过新凭证、标准化、质量门和脱敏日志但不接管用户响应

### Requirement: 不可绕过切换门槛
统一切换 MUST 要求零绕过清单、契约/fixture/迁移/安全/领域测试、全部启用 Capability 真实预检、每项启用能力至少一个健康合格 Instance、可选不可用能力显式停用、有效生效策略、凭证加密 readiness、后台演练和管理员 Go/No-Go 全部通过；任何管理员 MUST 不得强制绕过失败门槛。

#### Scenario: 一个启用 Capability 没有健康 Instance
- **WHEN** 其他检查全部通过但某启用 Capability 没有健康路由候选
- **THEN** Go/No-Go 失败且切换不得激活

### Requirement: 全应用维护窗口切换
生产切换 MUST 在有界全应用维护窗口进行：停止新用户流量和后台任务、排空活动操作、重新检查全部门槛、原子激活统一路由、验证 readiness 后才恢复服务。激活前失败 MUST 中止本次维护尝试。

#### Scenario: 排空期间仍有活动 Provider Attempt
- **WHEN** 维护窗口检查发现无法在截止时间前排空的外部调用
- **THEN** 切换在激活前中止且应用不得进入部分新路由状态

### Requirement: 一次性前向切换
全部范围内域 MUST 在生产同一切换点从旧功能内路由转到统一路由；目标版本 MUST 不包含运行时新旧路由开关、按领域回切或旧 fallback。激活后 MUST 不恢复旧版本，只能通过新路由、Capability 停用、合格缓存和前向代码修复处理事故。

#### Scenario: 激活后新路由出现故障
- **WHEN** 维护窗口已完成激活且某 Provider 路径随后失败
- **THEN** 运维只能在新体系内调整路由或向前修复，不得调用旧 provider/fallback 路径

### Requirement: API 兼容切换
全量迁移与切换 MUST 不要求现有 Web、策略、回测、Agent 或 MCP 消费者进行破坏性同步升级；任何不能保持兼容的现有不安全契约 MUST 在切换前单独提出并成为新的阻断决策。

#### Scenario: 旧客户端跨切换继续请求
- **WHEN** 使用既有请求和响应字段的客户端在切换后调用同一路由
- **THEN** 其原有功能继续工作且新增来源信息为可选内容

### Requirement: 新环境默认路由幂等初始化
系统 MUST 提供显式、幂等的默认数据路由初始化命令，在数据库迁移和注册表物化后创建代码注册的免密公共 Provider Instance，执行有界真实能力验证，仅为验证合格的 Instance 发布默认有序策略，并把无合格 Provider 的 Capability 显式停用。命令重复执行 MUST 保留管理员已发布、已起草或手工停用的策略，不得重复创建 Instance、覆盖凭据、伪造验证结果或写入真实秘密。

#### Scenario: 全新数据库首次初始化
- **WHEN** 注册表已物化但不存在 Provider Instance 或 Routing Policy
- **THEN** 命令创建稳定 Key 的免密公共 Instance、记录真实验证证据、发布合格默认路由并显式停用其余 Capability

#### Scenario: 重复执行初始化
- **WHEN** 同一环境再次执行默认路由初始化命令
- **THEN** 已配置 Instance、已发布策略和管理员草稿保持不变且不会产生重复记录

#### Scenario: 管理员已修改策略
- **WHEN** Capability 已存在管理员发布、草稿或非 bootstrap 原因的停用状态
- **THEN** 初始化命令保留管理员状态且只在结果中报告跳过
