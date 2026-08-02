## Context

QuantDinger 当前的外部数据调用分布在 `app/data_sources/`、`app/data_providers/`、市场同步服务、基本面服务和若干功能模块中。多个模块直接声明 Twelve Data、腾讯、yfinance、AkShare、CCXT、FRED、BLS、BEA、Finnhub、Trading Economics、搜索服务等优先级与 fallback；配额、缓存、重试、错误分类和健康状态也由各模块局部实现。现有 `ProviderAttempt` 提供脱敏、fail-open 明细日志，但没有一条拥有策略修订、缓存决策和完整 fallback 结果的 `Routed Data Request` 控制记录。

现有数据源秘密主要来自 `.env`/进程环境和 addon config，`app/utils/config_loader.py` 还明确禁止从数据库读取秘密；另一方面，项目已具有基于 `CREDENTIAL_ENCRYPTION_KEY` 的持久化凭证加密工具。新设计需要把数据源凭证迁入数据库而不影响经纪商/交易边界，并支持专用密钥轮换。

现有后台任务已迁入内部 Task Scheduler，机器仍只有 2 个逻辑 CPU、约 3.5 GiB 内存。设计不得新增消息代理、数据路由容器或高并发探测器；迁移、重新加密、健康探测和日志清理必须注册为有限 Task Definition，按低并发、有界批次执行。

Web 管理源代码位于独立仓库 `/home/quantadinger/QuantDinger-Vue`。Mobile/H5 不承担管理员数据源运维页面。生产切换按已确认决策停止整个应用、一次性激活全部范围内路由并只允许向前修复；这是高风险且难以逆转的架构选择，必须单独记录 ADR。

## Goals / Non-Goals

**Goals:**

- 建立代码注册、版本化且可验证的 Provider Adapter 与 Data Capability 控制面。
- 让 Provider Instance 的身份、凭证、能力资格、配额、健康、熔断和生命周期具有统一持久化语义。
- 让所有外部数据获取通过同一 Data Routing Policy、Data Eligibility Constraint、质量门、缓存和来源记录执行。
- 保持现有业务 API 兼容，并让用户获得安全来源说明、管理员获得完整运维解释。
- 迁移全部范围内调用，删除旧功能内 provider/fallback 和旧凭证读取，不保留生产双路由。
- 在单机低资源约束下维持顺序构建、低频轮询、有界后台任务和无新增容器。

**Non-Goals:**

- 不支持第三方插件市场、动态 Python、任意 URL、Shell、SQL 或管理员路由表达式。
- 不把交易账户、持仓、订单和 broker/exchange execution 纳入数据路由；同一 SDK 的公开行情调用仍纳入。
- 不建立跨 Provider 自动对账、修正或通用数据治理平台。
- 不让普通用户选择 Provider 或查看内部路由、配额、健康和 Attempt。
- 不改变 A 股回测只读合格本地历史库、不得远程补洞的既有业务契约。
- 不为生产提供逐领域切换、影子复制全部流量、运行时旧路由 fallback 或版本回滚。

## Decisions

### 1. 新建独立 data routing service boundary

在 `backend_api_python/app/services/data_routing/` 建立控制面与执行面模块，建议拆为 `registry.py`、`models.py`、`repository.py`、`credentials.py`、`policy.py`、`router.py`、`cache.py`、`quality.py`、`quota.py`、`health.py`、`diagnostics.py`、`audit.py` 和 `inventory.py`。现有 `app/data_sources`/`app/data_providers` 保留 Provider 特有协议、字段映射和底层节点逻辑，但只通过注册 Adapter 暴露，不再拥有跨 Provider 优先级。

选择 service boundary 而不是在 `factory.py` 上继续叠加，是为了保持现有架构规则：Adapter 负责第三方协议与标准化，service 负责 use-case、fallback、错误和用户可见语义。Adapter 不接触 Flask、用户、JWT 或前端字段。

### 2. 注册表是 Adapter 与 Capability 的真相来源

注册项包含稳定 Key、版本、非秘密配置 Schema、凭证 Schema、支持 Capability、账户身份解析器、配额模型、成本估算器、诊断方法、标准化函数和可分类错误。Capability 注册项包含市场/数据语义、允许约束维度、标准结果契约、硬质量门、严格档位、缓存 Key 组成和时效策略。

启动时注册表幂等物化元数据。数据库不能创建新 Key。代码删除或不兼容升级若仍被生效引用，只使 data routing readiness 失败，不阻止无关 Flask 管理/诊断端点启动，以便管理员查看阻断原因；业务数据端点在路由未 Ready 时返回稳定不可用错误。

### 3. Provider Instance 与上游账户一一对应

Provider Instance 状态建议为 `draft`、`validation_failed`、`active`、`disabled`、`migration_required`、`retired`。激活至少需要一项 Capability 验证成功。Adapter 优先通过 Provider 账户/profile/plan 接口产生非秘密 `provider_account_identity`；数据库对 `adapter_key + provider_account_identity` 的非退役记录建立唯一约束。

对无法取得稳定账户身份的 Provider，仅允许一个活动 Instance，避免两个 API Key 实际共享账户配额却被当成独立预算。完全相同秘密可通过独立 HMAC/pepper 生成不可逆比较标记来拒绝重复，但比较标记不得替代 Provider Account Identity、不得用于显示或恢复秘密。

凭证轮换必须保持同一 Provider Account Identity；如果新凭证验证为另一个上游账户，系统拒绝轮换并要求创建新的 Provider Instance。这样历史健康、配额、路由和审计不会在同一个 Instance ID 下跨账户串接。

### 4. 凭证使用版本记录和 key-id envelope

新增凭证记录保存 Instance、Schema 版本、状态 `pending/active`、`encryption_key_id`、ciphertext、创建者和时间，不保存明文历史。活动与待处理版本最多各一个。轮换先写 pending，完成 Adapter 验证后在同一事务中交换 active 并删除旧 ciphertext；失败 pending 可被新提交覆盖或显式丢弃。

扩展现有 `credential_crypto` 为 keyring：一个 active key 和有限 previous decrypt-only keys，ciphertext 明确记录 key id。Task Scheduler 注册串行重加密任务，按小批次 `SELECT ... FOR UPDATE SKIP LOCKED` 迁移并验证；日志只记录记录 ID、key id 和结果。旧 `SECRET_KEY` fallback 只用于既有非本变更密文兼容，不成为新 Provider 凭证的常规 key source。

### 5. PostgreSQL 保存控制事实，进程使用不可变快照

建议新增：

- `qd_data_adapter_types`、`qd_data_capabilities`：代码注册元数据与版本墓碑。
- `qd_provider_instances`、`qd_provider_credentials`、`qd_provider_instance_capabilities`：实例、秘密版本与能力资格。
- `qd_provider_quota_states`、`qd_provider_health_states`、`qd_provider_health_evidence`：配额、健康/熔断与有界证据。
- `qd_data_routing_policies`、`qd_data_routing_revisions`、`qd_data_routing_entries`：草稿、生效版本与顺序。
- `qd_routed_data_requests`、扩展后的 `qd_external_data_request_logs`：请求汇总和 Attempt 关联。
- `qd_data_source_audit`、`qd_data_routing_cutovers`：失败关闭管理审计和切换门槛。

发布策略、禁用、隔离和凭证切换在 PostgreSQL 事务中更新版本。进程按版本号低频轮询并构造不可变 `RoutingSnapshot`，验证完成后原子替换引用；不在每个请求中重新拼接数据库对象。Snapshot 只包含非秘密配置、ID、资格、策略和 `SecretHandle`，不得序列化或输出解密凭证；受控进程内 SecretStore 仅缓存活动秘密的短生命周期内存对象。已有进程数据库中断时可在这些已加载 secret handle 仍有效的有界 degraded window 内继续，冻结管理面；新进程无快照不 Ready。

### 6. Routed Data Request 拥有完整执行结果

业务服务调用 `DataRouter.execute(capability_key, subject, constraints, mode, calling_feature)`。Router 固化策略修订、请求 deadline、约束和 Calling Feature，依次：

1. 校验 Capability 与允许的约束维度。
2. 检查合格新鲜 Routing Cache。
3. 从策略顺序中按当前 Instance 生命周期、能力资格、健康、配额和请求约束筛选 Attempt。
4. 调用 Adapter、执行有限 transport retry、标准化并运行质量门。
5. 首个通过硬门的结果写入 Routing Cache 并完成请求。
6. 无 Provider 成功时检查合格陈旧缓存，否则返回结构化失败。

`RoutedDataResult` 统一包含 data、request ID、provider public name、acquired_at、freshness、quality warnings 和内部 provenance。现有 route/service 适配层把新增字段放入可选 envelope 字段或响应头，保持旧 payload 兼容。

### 7. Routing Cache 不吸收持久化历史库

Routing Cache 复用现有 Redis/进程缓存能力，但使用 Capability 注册项构造完整 Key，并保存原 Instance、获取时间、质量档位与内容摘要。缓存命中创建 Routed Request 汇总但零 Attempt。A 股 PostgreSQL 日线、基本面、公司行为和回测数据继续由领域 repository 管理，不迁为 Redis cache，也不因缓存策略改变其 point-in-time、版本或覆盖语义。

### 8. 后台任务按 Capability stream 固定来源

Task Run 可以协调多个 Capability stream；每个 stream 在实质写入前固化策略修订与 Instance，并写入领域检查点。例如 CN history 同步可分别固定 easy_tdx 日线与官方公司行为引用 Provider，但日线 stream 自身不得在已提交部分数据后静默换源。

Adapter 提供下一 commit-safe chunk 的保守成本估计。Task 在开始 chunk 前验证/预留配额，配额不足则 `retry_wait` 或延期；Instance 禁用后完成当前安全 chunk 即停止。需要换源时创建明确的新 Provider Attempt 或新 Task Run，并由领域逻辑决定从哪个检查点重启。

### 9. 健康证据按 scope 分类

`HealthScope` 为 instance 或 instance+capability。Adapter 错误分类声明 permanence、scope hint、retryability 和 quota effect；未知错误默认 capability scope。永久认证/暂停/配置错误立即打开相应 circuit；临时错误进入固定窗口计数器，达到 Capability 注册阈值与最小样本量后打开。

恢复探测由 Task Scheduler 以低频有限任务执行，不使用用户请求。管理员 Capability Test 写入诊断记录但不更新生产 circuit；管理员触发 recovery probe 才进入 circuit recovery 状态。管理员可 quarantine 或延长 open，不可 force-close。

### 10. 配额使用 Adapter 模型与原子预算更新

Adapter 描述 bucket、单位、endpoint weight、reset 和响应观测解析。管理员配置套餐上限，响应 header/metadata 更新可信观测，Router 取保守值。数据库可用时通过带版本/条件更新的原子 reservation 协调多进程，并预先向各进程发放有期限、不可重叠的保守 safety slice；数据库 degraded window 内只能消费 outage 前已获得且未过期的 slice，没有可验证 slice 时停止相应外部调用，禁止现场推导新预算或把未知容量当成无限。

免费或无正式配额接口的 Provider 必须在 Adapter 中声明安全预算和低并发默认值；无法声明则不可路由。交互请求只在 reset 落入短 wait budget 时等待一次，否则 skipped 并 fallback。

### 11. 运维日志 fail-open，管理审计 fail-closed

扩展 `ProviderAttempt` 接受 routed request ID、instance ID、policy revision、attempt order、skip reason、quality outcome 和 bounded retry summary。Attempt 与 Routed summary 写入继续 best-effort；写失败不改变 Provider 结果，但设置 degraded-observability metric/health。成功 Attempt 默认 30 天，异常 Attempt 与 Routed summary 默认 90 天，由注册 Task Definition 有界清理。

Instance、凭证、路由、权限、隔离和切换操作通过独立 audit repository 与业务变更同事务提交；审计失败则整个管理操作失败。审计保存脱敏 diff、actor、reason、correlation ID 和 outcome，不复用普通应用日志作为唯一证据。

### 12. 细分权限与 Step-up 不依赖前端

新增后端权限代码，例如 `data_sources:view`、`data_sources:instances`、`data_sources:credentials`、`data_sources:routing`、`data_sources:diagnostics`、`data_sources:cutover`。超级管理员默认全有；权限检查在 API service boundary 执行。

凭证提交/轮换、权限分配、Instance 退役和 cutover 要求近期密码或 MFA Step-up token。路由发布等高影响操作要求 reason，但不要求每次 Step-up。所有 API response 都使用现有错误 envelope 与国际化约定。

### 13. 控制台复用现有页面而不重复功能

Vue 新增一个 admin route 和 Overview、Provider Instances、Routing Policies 三标签。External Data Request Logs 继续独立，但增加 Routed ID 分组/跳转；Data Source Settings 移除数据源凭证编辑，保留共享非秘密 transport 与 legacy import 状态。A 股覆盖、质量、批次仍留在领域页面，通过 Routed ID/Instance deep link 连接控制台。

第一版使用低频轮询和稳定 cursor/page contract，不引入 SSE。Mobile/H5 不新增管理 UI。前端必须在旧 backend 或 data router not-ready 时显示不可用，不能伪造绿色状态。

### 14. 全量 inventory 是代码和切换契约

建立机器可检查 manifest，把所有 in-scope 调用映射到 Adapter、Capability、Calling Feature 与 owner module。静态检查扫描 `requests/httpx/aiohttp/urllib`、SDK client 和已知第三方数据库调用；允许列表只包含非数据服务与 Adapter 内部实现。测试覆盖 manifest 中每个 Calling Feature 至少一条 Router 入口。

迁移按代码领域顺序实施以控制变更，但不在生产逐领域切流。为满足真实 preflight，可以部署有界的 preflight release：旧业务入口仍使用旧选择逻辑，新控制面和显式管理员诊断只允许直接调用新 Adapter contract，两者之间没有“新路由失败再回旧路由”或按请求切换的 runtime fallback。最终 cutover artifact 删除旧实现。

### 15. 一次性全应用、前向切换

切换前执行旧凭证显式导入、全部真实 Capability preflight、非提交后台演练、数据库迁移、前后端契约测试、inventory 零绕过和 readiness 验证。切换在全应用 maintenance 中停止入口和 workers、排空 Attempt、最终复核 gate、激活唯一 cutover record、启动不含旧路由的目标版本并验证。

激活前任何失败都中止维护尝试；激活后不恢复旧 artifact、不提供 runtime legacy switch。事故只能通过新路由策略、Capability disablement、合格缓存和 forward fix 处理。此决策必须新建 ADR，明确风险、替代方案与操作手册。

### 16. easy_tdx 继续遵守本地 wheel 基线

easy_tdx 仍作为 Provider Adapter 的内部依赖，候选节点属于 Adapter 内部 transport/health 细节而不是管理员可创建的 Provider Instance。开发期继续使用 `/home/quantadinger/easy_tdx` 基线 1.20.4、提交 `3b7ce97` 构建的 wheel；本变更不假设公共包已发布，不复制 sibling 源码、不用 editable install 或运行时挂载。

## Risks / Trade-offs

- [全量第一版范围极大，容易形成不可审阅的大改动] → 任务按注册表/数据模型、执行面、各数据域迁移、UI、安全和切换门槛拆成顺序阶段，每阶段有 inventory 与定向测试，但生产只在全部完成后切换。
- [一次性前向切换没有软件回退] → 全应用维护、真实 preflight、零绕过 inventory、不可绕过 gate、完整演练和独立 ADR；激活前失败全部中止。
- [同一 Provider 的不同 API Key 可能共享无法识别的账户配额] → 能识别账户时数据库唯一约束；不能识别时限制一个活动 Instance。
- [数据库 outage 下多进程配额可能不一致] → degraded window 使用更小的进程 safety slice，冻结配置，并在窗口到期后停止外部调用而不是无界继续。
- [运维日志 fail-open 会留下调查缺口] → 显式 degraded-observability health/metric、Routed ID 响应标记和告警；管理审计仍 fail-closed。
- [旧接口 payload 多样，统一 provenance 容易破坏客户端] → 逐 endpoint contract test，优先响应头或可选 metadata，不改变已有必需字段和类型。
- [代码注册 Capability 可能产生过细枚举] → 只在 compatibility、normalized contract、quality gate 或 cache semantics 改变时拆分，普通 timeframe/adjustment 作为约束。
- [全部真实 preflight 消耗 Provider 配额] → 低频、代表性、Adapter 声明成本且不复制生产流量；预检计入真实 quota state。
- [新控制面表和日志增加磁盘占用] → 索引、摘要限长、成功 30 天/异常与 summary 90 天、有界清理和聚合长期指标。
- [凭证密文迁移失败可能阻断大量能力] → key id、逐批验证、旧 key 只解密保留到全量成功、Instance/Capability 级阻断说明。

## Migration Plan

1. 新建 ADR，记录“全量完成后、全应用维护、一次性激活、无运行时回切且切换后只向前修复”的不可逆决策。
2. 建立 registry、Capability taxonomy、inventory manifest、repository、权限/审计和凭证 keyring；提前应用与旧版本兼容的 additive 数据库迁移并部署 bounded preflight release，不接管生产业务调用。
3. 建立 Router、RoutingSnapshot、policy publication、constraints、cache、quality、quota、health/circuit、diagnostics 和 Routed Request/Attempt 契约。
4. 建立管理 API、readiness、维护任务和 Vue 三标签控制台；调整 Data Source Settings 与 External Data Request Logs 链接和权限。
5. 按领域迁移全部 Adapter 与 Calling Feature：A/H 股、A 股历史/公司行为/基本面、美股、crypto/CCXT、forex/futures、indices/commodities、macro/calendar、news/sentiment/opportunities、catalog/master data 及 inventory 发现的其他数据路径。
6. 为每个领域删除局部 provider priority/fallback，只保留 Adapter 内 bounded transport/node retry；完成 API compatibility、fixture、quality、quota、health 和后台 checkpoint 测试。
7. 在真实部署运行管理员确认的 legacy credential import、全部 Capability preflight 和非提交后台演练；验证所有启用 Capability 至少一个健康 Instance和有效策略。
8. 顺序构建并验证最终 backend 与 Vue artifact；运行零绕过 inventory、定向后端/前端/安全/迁移测试、Compose config 和 full maintenance rehearsal，证明已应用 Schema 与数据状态可由目标版本直接启动。
9. 停止整个应用，排空操作，最终复核不可绕过 gate，在不执行未演练 Schema 变更的前提下原子激活 cutover record，并部署不含旧路由与旧 secret lookup 的目标版本。
10. 只在新体系内恢复服务并监控 health、quota、fallback、cache、degraded observability 和磁盘；发现问题使用新路由/停用/forward fix，不恢复旧版本。

## Open Questions

无阻断问题。精确 timeout、poll interval、circuit threshold、degraded snapshot window、maintenance timeout、分页和批量大小在实施计划中按 Capability/Adapter 代码默认值确定，并必须具备有界测试；不得因此重新引入管理员任意表达式或绕过切换门槛。
