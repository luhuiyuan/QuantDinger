## Why

现有外部数据请求日志仅覆盖部分提供方。审计与线上复现确认，用户访问 A 股 `000012.SZ` 时实际调用腾讯行情获取 K 线，却没有留下日志；因此当前日志页面不能作为外部数据访问的完整审计依据。

## What Changes

- 将用户可直接触发的行情与市场数据回退链统一纳入外部请求日志边界。
- 为每次真实逻辑提供方尝试记录提供方、数据域、操作、回退序号、结果、耗时和脱敏摘要。
- 补齐各回退链的回归测试，覆盖前一提供方失败、后一提供方成功，以及成功、失败、禁用和空响应结果。
- 保持日志写入 fail-open，不改变既有业务响应、缓存语义或数据源优先级。

## Capabilities

### New Capabilities
- `external-data-request-observability`: 对用户直接触发的外部行情和市场数据请求提供完整、可审计的逻辑提供方尝试日志。

### Modified Capabilities

- 无。

## Impact

- 后端：`backend_api_python/app/data_sources/`、`backend_api_python/app/data_providers/`、`backend_api_python/app/services/market_data_collector.py` 及相关测试。
- 不新增公开 API 或数据库表；继续使用 `qd_external_data_request_logs` 和既有管理员日志页面。
- P0 范围包括 A/H 股、加密货币、外汇、期货、美股、指数、市场情绪与机会扫描等用户页面直接触发的数据链。
