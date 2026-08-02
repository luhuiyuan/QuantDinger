## ADDED Requirements

### Requirement: 代码注册 Adapter 与 Capability
系统 MUST 仅从可信应用代码注册具有稳定 Key 和显式版本的 `Provider Adapter` 与 `Data Capability`；数据库和管理 API 只能物化或引用注册项，不得创建任意 Adapter、Capability、端点或可执行集成逻辑。

#### Scenario: 管理员提交未知 Adapter
- **WHEN** 管理员尝试创建引用未注册 Adapter Key 的 Provider Instance
- **THEN** 后端拒绝请求且不创建可路由记录

#### Scenario: Capability 从代码移除但仍有生效引用
- **WHEN** 新版本注册表缺少仍被生效策略引用的 Capability
- **THEN** 数据路由 readiness 失败并列出阻断引用

### Requirement: Provider Instance 生命周期
系统 SHALL 支持 Provider Instance 草稿、激活、禁用和永久退役；草稿在从未激活、从未进入生效路由且无运维历史时可丢弃，激活后的 Instance MUST 不得硬删除。

#### Scenario: 丢弃未使用草稿
- **WHEN** 管理员丢弃从未激活且无历史的 Instance 草稿
- **THEN** 系统删除草稿且不留下可路由身份

#### Scenario: 退役已使用 Instance
- **WHEN** Instance 已禁用、移出全部生效路由并排空活动操作
- **THEN** 系统允许退役、销毁当前凭证密文并保留非敏感历史身份

### Requirement: 数据库加密与只写凭证
Provider Instance 凭证 MUST 使用独立凭证加密密钥加密后存入数据库；保存后任何 API 和界面 MUST 不得返回明文或可恢复密文，管理员只能查看配置状态、安全账户元数据并提交替换值。

#### Scenario: 读取已保存凭证
- **WHEN** 具有最高管理员权限的调用方读取 Provider Instance
- **THEN** 响应只返回字段配置状态和脱敏元数据且不包含凭证明文

#### Scenario: 日志包含凭证字段
- **WHEN** 凭证提交、验证或失败产生应用日志、Task Event 或管理审计
- **THEN** 所有秘密字段被移除或固定脱敏且不可从记录恢复

### Requirement: 原子凭证轮换
凭证轮换 MUST 把替换值保存为待处理密文，在旧凭证继续生效时完成 Schema、Provider Account Identity 与 Adapter 能力验证；新凭证 MUST 属于同一上游账户，验证成功后 MUST 原子切换并立即销毁旧密文，验证失败或账户身份变化 MUST 不影响当前生产凭证。

#### Scenario: 新凭证验证失败
- **WHEN** 替换凭证无法通过认证或能力测试
- **THEN** 旧凭证继续服务且新凭证保持非活动状态供替换或丢弃

#### Scenario: 新凭证验证成功
- **WHEN** 待处理凭证通过所有必要验证
- **THEN** 系统原子激活新版本、销毁旧密文并仅保留非敏感轮换审计

#### Scenario: 新凭证属于另一个上游账户
- **WHEN** 待处理凭证验证得到与当前 Instance 不同的 Provider Account Identity
- **THEN** 系统拒绝轮换并要求管理员为新账户创建独立 Provider Instance

### Requirement: 上游账户唯一性
同一 Adapter 下的一个上游账户或 quota owner MUST 最多对应一个非退役 Provider Instance。Adapter 能取得稳定非敏感 `Provider Account Identity` 时 MUST 用它拒绝重复激活；无法可靠识别账户的 Adapter MUST 最多允许一个活动 Instance。

#### Scenario: 第二个 Instance 使用相同账户
- **WHEN** 新 Instance 验证得到的 Provider Account Identity 已属于另一非退役 Instance
- **THEN** 激活被拒绝且不创建第二个独立配额身份

#### Scenario: Provider 无法返回账户身份
- **WHEN** Adapter 无法证明不同凭证属于不同 quota owner
- **THEN** 系统只允许该 Adapter 存在一个活动 Instance

### Requirement: Instance 能力资格
Provider Adapter MUST 声明最大 Capability 集合，Provider Instance MUST 通过凭证、套餐、区域和能力级验证形成实际可用子集；至少一项 Capability 验证成功后才能激活，管理员不得手工授予未验证资格。

#### Scenario: 仅部分能力可用
- **WHEN** Instance 的账户套餐只通过 Adapter 声明能力中的一部分验证
- **THEN** Instance 可激活但只对验证成功的 Capability 具有路由资格

#### Scenario: 权限被 Provider 撤销
- **WHEN** Provider 明确证明账户已失去某项能力权限
- **THEN** 系统移除该能力资格直至之后重新验证成功

### Requirement: 能力资格重新验证
系统 SHALL 在凭证或相关配置变化、收到套餐/权限变化证据以及低频周期检查时重新验证 Instance Capability；普通临时故障 MUST 只影响健康和熔断，不得直接擦除长期资格。

#### Scenario: 临时超时
- **WHEN** 已验证 Capability 发生一次网络超时
- **THEN** 系统更新健康证据但保留其能力资格

### Requirement: 配额模型与未知安全预算
Adapter MUST 定义配额维度、单位、权重和重置语义；系统 SHALL 结合管理员套餐配置与可信响应元数据采用保守有效值。无法发现上限时 Adapter MUST 提供保守安全预算，否则 Instance 不得路由；未知 MUST 不得视为无限。

#### Scenario: Provider 返回剩余额度
- **WHEN** 响应包含可信的剩余额度和重置时间且低于管理员配置值
- **THEN** 路由使用响应观测的更保守额度

#### Scenario: 无法定义未知配额安全预算
- **WHEN** Adapter 无法发现限制且不能声明安全预算
- **THEN** Instance 不获得路由资格并在控制台显示原因

### Requirement: Adapter 版本迁移与移除门槛
Adapter MUST 具有稳定 Key 和显式契约/配置版本，并为可安全转换的非秘密配置提供确定性迁移。需要人工判断或新凭证的 Instance MUST 进入 `migration_required` 非路由状态；仍被生效策略引用时 MUST 阻止 readiness。Adapter 代码 MUST 仅在全部 Instance 退役且策略迁移后移除。

#### Scenario: 升级需要新凭证字段
- **WHEN** 新 Adapter 版本新增无法自动产生的必要秘密字段
- **THEN** 相关 Instance 进入 `migration_required` 且在管理员补充并验证前不可路由

#### Scenario: 删除仍有活动引用的 Adapter
- **WHEN** 部署版本缺少仍被非退役 Instance 或生效策略引用的 Adapter
- **THEN** 数据路由不进入 Ready 且保留不可执行元数据用于解释历史

### Requirement: 凭证加密主密钥轮换
系统 MUST 支持新活动密钥与旧只解密密钥的有界过渡，并通过串行有界任务把现有当前和待处理凭证在内存中重新加密；旧密钥 MUST 仅在全部密文验证可由新密钥解密后移除。

#### Scenario: 分批重新加密
- **WHEN** 管理员开始凭证主密钥轮换
- **THEN** 新写入只使用新密钥且后台任务按有界批次迁移旧密文而不记录明文

#### Scenario: 缺少旧密钥
- **WHEN** 活动 Instance 的密文无法由已配置密钥解密
- **THEN** 受影响路由不进入 Ready并报告不可恢复的凭证阻断
