## ADDED Requirements

### Requirement: 用户行情数据链记录全部逻辑提供方尝试
系统 SHALL 为用户直接触发的 K 线、报价、全球市场概览、指数、热图、情绪和机会扫描链中的每次真实外部逻辑提供方尝试写入外部请求日志。日志 MUST 包含提供方、数据域、操作、回退序号、耗时和结果，且不得保存凭据或完整请求 URL。

#### Scenario: A 股日线由腾讯行情提供
- **WHEN** 本地 A 股历史数据不可用、Twelve Data 未返回数据且腾讯行情成功返回日线
- **THEN** 系统 MUST 记录腾讯行情的成功尝试，并将其标记为该回退链对应的 `fallback_index`

#### Scenario: 前序提供方失败后回退成功
- **WHEN** 一条用户行情回退链中的前序提供方失败而后序提供方返回可用数据
- **THEN** 系统 MUST 分别记录失败与成功的逻辑提供方尝试，且后序记录的 `fallback_index` 大于前序记录

### Requirement: 未配置和空响应可审计
系统 MUST 将用户行情链中已评估但未实际执行的禁用提供方记录为 `disabled` 或 `skipped`，并将真实请求的无可用数据响应记录为 `invalid_response`，同时保持业务回退继续执行。

#### Scenario: 未配置 Twelve Data
- **WHEN** K 线链评估 Twelve Data 且 API Key 未配置
- **THEN** 系统 MUST 记录一条不含密钥的 `disabled` 或 `skipped` 尝试，并继续后续提供方

### Requirement: 外部日志不得阻断行情响应
系统 MUST 保持外部请求日志写入 fail-open；日志数据库错误、序列化错误或清理任务失败不得改变用户行情请求的响应结果。

#### Scenario: 日志写入失败
- **WHEN** 外部提供方成功返回数据但日志写入失败
- **THEN** 系统 MUST 向调用方返回原始行情数据，并仅在服务端记录安全的诊断信息

### Requirement: 回退链具备回归测试
系统 MUST 为每类 P0 用户行情回退链提供定向测试，验证提供方、结果分类和回退序号；测试不得访问真实外部网络。

#### Scenario: 腾讯 K 线回退测试
- **WHEN** Twelve Data mock 返回空数据且腾讯 K 线 mock 返回可用日线
- **THEN** 测试 MUST 断言腾讯提供方成功日志存在且 yfinance 未被调用
