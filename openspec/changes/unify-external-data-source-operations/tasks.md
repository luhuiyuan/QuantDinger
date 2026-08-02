## 1. 架构决策与全量调用盘点

- [x] 1.1 新建 ADR，记录全量完成后停止整个应用、一次性激活统一路由、无运行时回切且激活后只向前修复的决策、替代方案和风险
- [x] 1.2 建立 in-scope/out-of-scope outbound call inventory manifest，记录 owner module、Calling Feature、Adapter、Capability、凭证来源和迁移状态
- [x] 1.3 实现静态扫描检查，识别 `requests/httpx/aiohttp/urllib`、SDK client 和已知第三方数据调用，并要求每个范围内调用映射到 manifest
- [x] 1.4 为 inventory 增加允许列表，只允许交易、LLM、通知、支付、OAuth、基础设施调用和注册 Adapter 内部 transport
- [x] 1.5 添加零绕过测试，证明所有范围内 Calling Feature 至少存在一个统一 Router 入口且旧功能内 fallback 不可作为可接受遗留

## 2. 数据模型与数据库迁移

- [x] 2.1 设计并迁移 Adapter/Capability 物化与版本墓碑表，加入稳定 Key、版本和注册状态约束
- [x] 2.2 设计并迁移 Provider Instance、Provider Account Identity、非退役账户唯一性和生命周期表/约束
- [x] 2.3 设计并迁移 Provider credential 当前/待处理版本、key id、Schema 版本和单活动版本约束
- [x] 2.4 设计并迁移 Instance Capability 资格、验证证据、禁用状态和最近验证时间
- [x] 2.5 设计并迁移 quota state/reservation、health/circuit/evidence 与 capability/instance scope 索引
- [x] 2.6 设计并迁移 routing policy、draft/effective revision、ordered entry 和不可变历史表
- [x] 2.7 设计并迁移 Routed Data Request summary，并扩展 External Data Request Log 关联 request、instance、revision、attempt order 和 skip/quality metadata
- [x] 2.8 设计并迁移 data source management audit、diagnostic、recovery probe 和 cutover gate/result 表
- [x] 2.9 为全部新表添加 FK、唯一约束、分页/留存索引、metadata 长度限制和向前迁移测试，证明 additive Schema 可在旧业务入口仍运行时提前部署

## 3. Adapter 与 Capability 注册表

- [x] 3.1 建立 `app/services/data_routing/` 模块边界和不依赖 Flask 的 typed contracts/errors
- [x] 3.2 实现代码 Provider Adapter registry，校验稳定 Key、版本、配置/凭证 Schema、Capability、quota、identity、diagnostic 和 normalization contract
- [x] 3.3 实现代码 Data Capability registry，声明市场/语义、允许约束、标准结果、硬质量门、严格档位和 Routing Cache Key 规则
- [x] 3.4 实现注册表幂等物化、版本差异检测、metadata tombstone 和启动一致性报告
- [x] 3.5 实现 Adapter 确定性非秘密配置迁移和 `migration_required` 处理
- [x] 3.6 实现 Adapter/Capability 缺失且仍有活动引用时的 data routing readiness 阻断
- [x] 3.7 添加注册 Schema、非法动态入口、版本升级、移除门槛和未引用待迁移 Instance 测试

## 4. Provider Instance 与凭证安全

- [x] 4.1 实现草稿创建、验证失败、激活、禁用、排空、退役和仅未使用草稿可丢弃的状态机/repository
- [x] 4.2 扩展 `credential_crypto` 为 active/previous key-id keyring，并禁止新 Provider 凭证使用普通 `SECRET_KEY` 作为常规来源
- [x] 4.3 实现 write-only Provider credential 创建、读取脱敏、pending replacement、同账户身份校验、原子激活和旧密文立即销毁；跨账户替换必须拒绝并要求新 Instance
- [x] 4.4 实现不可逆秘密比较标记以拒绝完全相同凭证复用，确保标记不可显示或恢复秘密
- [x] 4.5 实现 Adapter Provider Account Identity 验证与非退役唯一性；无法识别账户的 Adapter 限制一个活动 Instance
- [x] 4.6 实现 Instance Capability 验证、至少一项成功激活、管理员只能禁用不能手工授予资格
- [x] 4.7 实现配置/凭证变化、权限证据和低频周期任务触发的 Capability 重新验证
- [x] 4.8 注册串行有界凭证主密钥重加密 Task Definition，验证所有当前/待处理密文后才允许移除旧 key
- [x] 4.9 添加凭证不回显、日志/审计脱敏、轮换失败保留旧值、账户重复、单 Instance fallback 和 key rotation 故障测试

## 5. 路由策略控制面

- [x] 5.1 实现每 Capability 单一有序列表的 policy draft/revision repository 与后端校验
- [x] 5.2 实现草稿影响预览、显式 publish、事务内 management audit 和原子 effective revision 切换
- [x] 5.3 实现不可变 revision 历史和从旧 revision 生成新 draft 的恢复流程
- [x] 5.4 实现 Capability 显式停用、必填 reason、禁止空路由和无合格缓存时 `capability_disabled`
- [x] 5.5 实现不可变 `RoutingSnapshot` 构建、版本轮询和进程内原子替换，Snapshot 仅保存非秘密配置与不可序列化 `SecretHandle`
- [x] 5.6 实现请求创建时固化策略 revision，同时在每个未开始 Attempt 前重新检查 disable/quarantine/circuit 状态
- [x] 5.7 添加并发发布、无效 Instance、未验证 Capability、空路由、恢复重校验和 in-flight revision 测试

## 6. Routed Data Request 执行面

- [x] 6.1 实现 `DataRouter.execute` 请求 contract、稳定 Routed Data Request ID、Data Request Mode、Calling Feature 和 deadline
- [x] 6.2 实现 Data Eligibility Constraint 白名单校验和按策略顺序过滤，不支持管理员条件表达式或约束放宽
- [x] 6.3 实现 Adapter bounded transport retry、错误分类、标准化、Provider Attempt 顺序和 bounded retry summary
- [x] 6.4 实现代码通用硬质量门、Capability 硬门、管理员只可收紧的 profile/threshold 和结构化 warning
- [x] 6.5 实现首个硬门通过结果接受、硬门失败 fallback、warning 不额外调用 Provider 的执行规则
- [x] 6.6 实现统一 `RoutedDataResult` 来源、acquired_at、freshness、quality warning 和内部 provenance contract
- [x] 6.7 添加 constraints、fallback、deadline、skip、quality gate、warning 和请求解释的针对性测试

## 7. Routing Cache

- [x] 7.1 定义并实现 Capability 驱动的完整 Routing Cache Key、来源、获取时间、质量档位和内容摘要
- [x] 7.2 复用现有 Redis/进程缓存边界，明确排除 PostgreSQL 历史、基本面、公司行为和回测数据集
- [x] 7.3 实现交互请求新鲜缓存 → Provider 列表 → 合格陈旧缓存顺序和 stale 响应标识
- [x] 7.4 实现 Capability 停用时只允许符合 mode/constraints 的缓存且零 Provider Attempt
- [x] 7.5 添加语义 Key 隔离、fresh/stale、约束不放宽、缓存 provenance 和零 Attempt 测试

## 8. 配额控制

- [x] 8.1 实现 Adapter quota bucket、单位、weight、reset、response observation 和 conservative merge contract
- [x] 8.2 实现管理员 plan limit、可信 header/metadata 更新和 unknown-limit safety budget
- [x] 8.3 实现数据库可用时的原子 quota reservation/consume/release、跨进程一致性和有期限不重叠 safety slice 预分配
- [x] 8.4 实现 control-plane degraded window 只消费 outage 前已分配且未过期的 conservative safety slice，无有效 slice 时停止调用
- [x] 8.5 实现交互请求 reset 在短预算内等待一次，否则记录 `quota_exhausted` skipped Attempt并 fallback
- [x] 8.6 实现后台 Capability stream 下一 commit-safe chunk 成本估算、预算验证/预留、等待或延期
- [x] 8.7 添加 quota observation 冲突、unknown safety、跨进程 reservation、交互等待和后台 chunk 测试

## 9. Provider Health、熔断与诊断

- [x] 9.1 实现 instance 与 instance+capability health state、证据分类和未知故障默认 capability scope
- [x] 9.2 实现永久故障立即开 circuit、临时故障滚动窗口/最小样本量开 circuit
- [x] 9.3 实现低频专用 recovery probe、cooldown、有界 backoff 和成功证据关闭 circuit
- [x] 9.4 实现管理员 quarantine、circuit extension、提前 recovery probe，并拒绝 force-close/force healthy
- [x] 9.5 实现 Provider Capability Test 绕过 routing/fallback 但保留 credentials/quota/normalization/quality/sanitization/logging
- [x] 9.6 实现 Instance disable/quarantine 阻止新 Attempt、已发调用完成、后台 stream 安全 chunk 后停止
- [x] 9.7 实现低频健康与资格检查 Task Definition，保持当前低资源全局串行策略
- [x] 9.8 添加 scope escalation、permanent/transient circuit、diagnostic 不改 circuit、recovery 和并发隔离测试

## 10. 可观测性、留存与最后有效快照

- [x] 10.1 扩展 `ProviderAttempt` 与服务，记录 routed request、instance、revision、order、skip、retry、quality 和 sanitized outcome
- [x] 10.2 实现 Routed Data Request summary，包括 cache/provider/failure 最终选择和策略解释
- [x] 10.3 保持 request/attempt operational logging fail-open，并新增 degraded-observability metric、health 和告警
- [x] 10.4 实现成功 Attempt 30 天、异常 Attempt/summary 90 天的可配置有界清理任务与测试
- [x] 10.5 实现最后有效 RoutingSnapshot、数据库 outage 管理冻结、stale control-plane health 和有界继续窗口
- [x] 10.6 实现无有效快照的新/重启进程 readiness 失败，禁止读取旧环境配置作为路由 fallback
- [x] 10.7 添加日志写失败不改变 Provider 结果、留存批次、snapshot 过期和进程重启测试

## 11. 权限、Step-up、审计与管理 API

- [x] 11.1 定义并实现 `data_sources:view/instances/credentials/routing/diagnostics/cutover` 后端权限和超级管理员默认映射
- [x] 11.2 实现近期 password/MFA Step-up proof，并应用于凭证、权限、退役和 cutover API
- [x] 11.3 实现高影响操作 reason 校验和 sanitized before/after management audit
- [x] 11.4 让管理变更与 audit 在同一事务中失败关闭，覆盖 audit 写失败与并发冲突
- [x] 11.5 实现 Overview、Provider Instances、Routing Policies、diagnostics、logs/deep-link 和 readiness 管理 API
- [x] 11.6 更新主 OpenAPI 与相关导出契约，保持业务 API 旧字段和错误 envelope 兼容
- [x] 11.7 添加普通用户拒绝、细分权限、Step-up 过期、reason 缺失、审计失败和秘密脱敏 API 测试

## 12. Web Data Source Operations Console

- [x] 12.1 在 `/home/quantadinger/QuantDinger-Vue` 新增管理员 Data Source Operations route、API client 和权限 guard
- [x] 12.2 实现 Overview 标签的 readiness、incident、circuit、fallback trend、quota 和待处理项视图
- [x] 12.3 实现 Provider Instances 列表/详情、草稿、验证、激活、禁用、退役 blocker、凭证轮换和 Capability 状态
- [x] 12.4 实现 diagnostic、quarantine、recovery probe、quota/health evidence 和安全确认交互
- [x] 12.5 实现 Routing Policies 生效列表、draft 编辑、排序、校验、影响预览、reason、publish、disable 和 revision restore
- [x] 12.6 调整 Data Source Settings，只保留共享非秘密 transport 与 legacy import 状态并移除 Instance credential 编辑
- [x] 12.7 扩展 External Data Request Logs 的 Routed ID 分组/跳转，保持为独立页面且不复制控制台标签
- [x] 12.8 添加中英文文案、旧 backend/not-ready 降级、普通用户不可见和稳定分页/轮询测试
- [x] 12.9 使用低内存 Node 配置对 Vue 执行定向 lint/type/build 验证；不构建 Mobile/H5

## 13. A/H 股行情与 A 股历史迁移

- [x] 13.1 注册 Twelve Data、腾讯、yfinance、AkShare 的 A/H 股 K 线 Adapter/Instance Schema/Capability 和配额/质量 contract
- [x] 13.2 将 `asia_stock_kline`、`cn_stock`、`hk_stock` 的硬编码优先级迁到 Data Routing Policy并删除跨 Provider fallback
- [x] 13.3 注册 A/H 股报价与全市场快照相关 Adapter/Capability并迁移页面、策略和 API Calling Feature
- [x] 13.4 注册 easy_tdx 日线/公司行为 Adapter，保留候选节点作为 Adapter 内部 transport/health 而非 Provider Instance
- [x] 13.5 继续使用 `/home/quantadinger/easy_tdx` 1.20.4、提交 `3b7ce97` 构建的本地 wheel 进行开发验证，不复制源码、不用 editable install、不假设公共制品
- [x] 13.6 将 CN history Task Run 按日线与官方公司行为引用 Capability stream 固定来源、revision 和检查点
- [x] 13.7 迁移 A/H 股 cache、日志、quality、quota 和 health 测试并保持 A 股历史数据库/回测契约不变

## 14. 基本面、公司行为与市场目录迁移

- [x] 14.1 注册东方财富年度基本面、巨潮核验与相关 A/H 基本面 Adapter/Capability
- [x] 14.2 将基本面增量/回填 Task Run 接入固定 Capability stream、配额分块、质量门和 Routed ID
- [x] 14.3 注册交易所/巨潮公司行为官方引用与现有 provider，并保持来源证据、point-in-time 和阻断质量规则
- [x] 14.4 迁移 symbol master、market catalog、instrument/status/calendar 等范围内外部目录调用
- [x] 14.5 删除这些领域的旧凭证 lookup 和跨 Provider fallback，并添加业务版本/证据不回退测试

## 15. 美股、Crypto、外汇与期货迁移

- [x] 15.1 注册美股 Finnhub/yfinance 等报价、K 线和基本面 Adapter/Capability并迁移 `us_stock` Calling Feature
- [x] 15.2 注册 CCXT 公开现货/永续行情 Capability，保持账户/持仓/订单调用在交易边界之外
- [x] 15.3 迁移 crypto price/K-line、CoinGecko/yfinance fallback 和 exchange public market data 到统一策略
- [x] 15.4 注册并迁移 Twelve Data、Tiingo、yfinance 等 forex Adapter/Capability 和所有 Calling Feature
- [x] 15.5 注册并迁移传统 futures 与公开 crypto derivatives market data，保留交易 execution 排除边界
- [x] 15.6 为美股、crypto、forex、futures 添加标准化、constraint、quota、health、cache 和 API compatibility 测试

## 16. 宏观、新闻与全局数据迁移

- [x] 16.1 注册并迁移 FRED、BLS、BEA macro series Adapter/Capability 和凭证/配额 Schema
- [x] 16.2 注册并迁移 Trading Economics、Finnhub、AkShare WallstreetCN economic calendar路由
- [x] 16.3 注册并迁移 commodity、indices、heatmap 和 global overview 数据 Provider
- [x] 16.4 注册并迁移 news/search service、sentiment、VIX/DXY/VXN/GVZ 与 opportunities 数据调用
- [x] 16.5 处理 inventory 发现的其他 analysis dataset 调用并明确任何 out-of-scope 例外依据
- [x] 16.6 删除各模块硬编码 Provider priority/fallback，保留 Adapter 内有限 transport retry并完成定向回归测试

## 17. 现有业务 API 与调用方兼容

- [x] 17.1 为每个迁移 Calling Feature 建立旧请求/成功 payload/领域错误 contract fixture
- [x] 17.2 以可选字段或响应头添加 Routed Data Request ID、公开 Provider、acquired_at、fresh/stale 和 warnings
- [x] 17.3 验证现有 Web 页面、策略运行、A 股回测、其他市场回测、Agent API 和 MCP 不需破坏性同步升级
- [x] 17.4 保持 A 股日线回测只读 PostgreSQL 合格历史库且不调用远程 Router 补洞
- [x] 17.5 为普通用户来源信息与管理员日志边界添加越权和隐私回归测试

## 18. 旧凭证导入与配置退役

- [x] 18.1 实现 supported legacy credential detector，读取环境/本地配置但从不通过 API 展示明文
- [x] 18.2 实现管理员 Step-up 确认的一次性导入、Adapter/Instance 映射、加密写入、逐 Capability 验证和脱敏审计
- [x] 18.3 实现导入状态、失败修复、手工替换和切换 blocker 管理 API/UI
- [x] 18.4 全部迁移验证后删除 `config_loader`/APIKeys 对范围内数据源秘密的生产读取路径
- [x] 18.5 更新 `env.example`、Data Source Settings、部署文档和秘密清理说明，不提交真实 `.env` 或凭证

## 19. Readiness、全应用维护与前向切换

- [x] 19.1 实现 cutover gate evaluator，覆盖 inventory、测试、preflight、健康 Instance、有效策略、显式停用、credential key、后台演练和 Go/No-Go
- [x] 19.2 确保任何管理员都无法绕过失败 gate，cutover Step-up、reason 和 management audit 必须失败关闭
- [x] 19.3 实现全应用 maintenance 状态、入口停止、Task/worker 停止、活动 Attempt/stream 排空和激活前 abort
- [x] 19.4 实现唯一 cutover record 和目标版本 readiness，要求 additive Schema/数据迁移在维护前已验证，激活后只允许新 Router且无 per-domain/legacy runtime switch
- [x] 19.5 删除全部旧功能内 provider/fallback、旧 secret lookup、兼容 adapter 和不可达配置分支
- [x] 19.6 编写并演练真实部署 preflight、全应用停止、最终 gate、数据库迁移、激活、启动和 forward-fix runbook
- [x] 19.7 添加激活前失败中止、激活后旧路径不可调用、无回滚入口和 forward-only 故障演练测试

## 20. 最终验证与文档

- [x] 20.1 运行 `python scripts/check_version.py`、`python scripts/check_mojibake.py` 和受影响 OpenSpec/文档静态检查
- [x] 20.2 从 `backend_api_python/` 运行受影响模块 compileall 和分领域定向 pytest，保持单进程且不运行 integration 标记
- [x] 20.3 运行凭证安全、权限审计、policy、cache、quota、health、inventory、API compatibility 和 cutover 的完整定向测试矩阵
- [x] 20.4 验证 normal Compose、GHCR Compose 和本地 UI override config；不执行并行或全栈 `up --build`
- [x] 20.5 按低内存规则检查 `free -h`、swap 和磁盘后，顺序构建 backend 与 Vue 受影响服务并执行最小运行验证
- [x] 20.6 更新架构、模块边界、API conventions、External Data Request Logs、部署、数据源运维和用户来源说明文档
- [x] 20.7 运行最终零绕过 inventory、全部启用 Capability preflight、非提交后台演练和不可绕过 Go/No-Go 报告生成
- [x] 20.8 确认 Mobile/H5 未增加管理页面或本地构建，正式 easy_tdx/镜像发布来源仍留待发布阶段决定
