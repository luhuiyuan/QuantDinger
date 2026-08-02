## ADDED Requirements

### Requirement: 三标签页管理员控制台
Web 管理端 MUST 提供 Data Source Operations Console，并包含 Overview、Provider Instances、Routing Policies 三个核心标签页；普通用户 MUST 不得访问页面或底层管理 API。

#### Scenario: 管理员打开 Overview
- **WHEN** 具有查看权限的管理员进入控制台
- **THEN** 页面显示 readiness、活动故障、开放熔断、fallback 趋势和待处理操作

#### Scenario: 普通用户访问控制台
- **WHEN** 非管理员访问控制台路由或管理 API
- **THEN** 系统拒绝访问且不返回敏感运维摘要

### Requirement: Provider Instance 管理工作流
Provider Instances 标签 MUST 支持草稿创建、验证、激活、禁用、排空后退役、凭证提交/轮换、Capability 资格、配额、健康、诊断、隔离和恢复探测，并 MUST 显示所有阻断操作的原因。

#### Scenario: Instance 无能力验证成功
- **WHEN** 管理员尝试激活零项 Capability 成功的草稿
- **THEN** 页面显示验证失败原因且后端拒绝激活

#### Scenario: 退役仍有策略引用的 Instance
- **WHEN** 管理员尝试退役仍在生效路由中的 Instance
- **THEN** 系统列出策略阻断且不提供强制退役

### Requirement: Routing Policies 管理工作流
Routing Policies 标签 MUST 支持按 Capability 查看生效路由、编辑草稿、完整校验、影响预览、显式停用、发布、不可变历史和从旧修订创建恢复草稿；空路由和无效 Instance 引用 MUST 不得发布。

#### Scenario: 预览路由发布影响
- **WHEN** 管理员准备发布改变主源顺序的有效草稿
- **THEN** 页面显示目标 Capability、新主源/fallback 顺序和验证结果后才允许发布

### Requirement: 设置与日志保持独立入口
Data Source Settings MUST 只管理共享非秘密传输参数和旧配置迁移状态，Provider Instance 凭证 MUST 不在该页面编辑。External Data Request Logs MUST 保持独立页面，并从控制台提供关联入口而不得复制成第四或第五个核心标签。

#### Scenario: 管理员轮换 Instance 凭证
- **WHEN** 管理员从 Instance 详情发起轮换
- **THEN** 操作在 Provider Instances 工作流完成且 Data Source Settings 不显示旧秘密值

### Requirement: 后端细分权限
系统 MUST 在后端分别校验运维查看、Instance 生命周期、凭证管理、路由发布和诊断/恢复权限；超级管理员 MAY 默认获得全部权限，但前端隐藏按钮 MUST 不得替代 API 授权。

#### Scenario: 只有查看权限的管理员发布路由
- **WHEN** 管理员具有运维查看权限但没有路由发布权限
- **THEN** 后端拒绝发布且不产生策略变更

### Requirement: Step-up 与变更理由
首次凭证提交、凭证轮换、权限分配、Instance 退役和不可逆生产切换 MUST 要求近期密码或 MFA Step-up。路由发布/恢复、Capability 停用、隔离、熔断延长、凭证轮换、Instance 禁用/退役和生产切换 MUST 要求操作理由。

#### Scenario: 过期会话轮换凭证
- **WHEN** 具有凭证权限的管理员会话未满足近期 Step-up
- **THEN** 后端要求重新认证且不保存替换凭证

#### Scenario: 发布路由未填写理由
- **WHEN** 管理员提交有效路由草稿但缺少操作理由
- **THEN** 后端拒绝发布且草稿保持未生效

### Requirement: 管理审计失败关闭
所有管理变更 MUST 在同一受控操作中持久化操作者、时间、理由、脱敏前后状态、correlation ID 和结果；必要审计无法写入时变更 MUST 不执行，审计 MUST 不包含凭证明文或可恢复密文。

#### Scenario: 审计数据库写入失败
- **WHEN** 管理员尝试禁用 Instance 且必要审计无法持久化
- **THEN** 禁用不生效并返回稳定审计失败错误

### Requirement: Routed Request 与 Attempt 留存
成功 Provider Attempt 明细 MUST 默认保留 30 天；失败、拒绝、禁用和跳过 Attempt 及 Routed Data Request 汇总 MUST 默认保留 90 天。期限 SHALL 可在安全有界范围内配置，长期趋势 MUST 使用聚合指标而不是无限保留逐请求详情。

#### Scenario: 成功明细超过期限
- **WHEN** 成功 Attempt 超过配置的 30 天默认期限
- **THEN** 有界清理任务删除明细且不删除仍在期限内的路由汇总

### Requirement: 管理员日志访问与用户来源分离
管理员 SHALL 可按 Routed Data Request ID 查看脱敏汇总和 Attempt 链；普通用户 MUST 只能在自己的业务响应中看到安全来源说明，不得查询运维日志。

#### Scenario: 管理员调查用户提供的 Routed ID
- **WHEN** 管理员用有效 Routed Data Request ID 查询近期问题
- **THEN** 系统返回策略修订、Attempt 顺序、缓存或最终失败原因的脱敏解释
