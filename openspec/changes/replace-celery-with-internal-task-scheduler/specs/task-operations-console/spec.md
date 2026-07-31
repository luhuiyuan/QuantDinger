## ADDED Requirements

### Requirement: 统一任务管理页面
Web 前端 MUST 提供统一 Task Management 页面，并包含 Overview、Schedules、Runs、Logs 四个标签页。Run MUST 可通过详情抽屉或详情页查看生命周期、进度、领域引用、重试关系、最后心跳、结果摘要和错误详情。

#### Scenario: 管理员查看失败回填
- **WHEN** 管理员从 Runs 标签页打开失败的基本面回填
- **THEN** 页面展示失败错误码、脱敏摘要、最后检查点、关联外部请求和可用人工操作

### Requirement: 管理员与任务所有者权限
管理员 MUST 可查看和管理所有注册任务、计划和 Run；普通用户 MUST 只能查看自己提交的用户任务，且不得管理系统计划或其他用户 Run。所有 API MUST 在后端执行权限校验，不得只依赖前端隐藏按钮。

#### Scenario: 普通用户查询他人任务
- **WHEN** 普通用户请求不属于自己的 Run 或事件
- **THEN** API 拒绝访问且不泄露任务是否存在

### Requirement: 计划管理与修订审计
管理员 MUST 能创建或修改注册任务的 Cron、时区、启用状态和经 Schema 校验的计划参数。暂停 MUST 只阻止未来计划触发，不影响运行中的 Run；修改 MUST 只影响未来触发并保留计划修订和管理员审计。

#### Scenario: 修改运行中任务对应的 Cron
- **WHEN** 管理员修改已有活动 Run 的计划 Cron
- **THEN** 活动 Run 不受影响，系统保存修订并按新配置计算下一次未来触发

#### Scenario: 暂停计划
- **WHEN** 管理员暂停计划而该计划已有运行中的 Run
- **THEN** 系统不再创建未来计划 Run，但运行中的 Run 继续执行

### Requirement: Run 启动、取消和 retry 操作
管理员 MUST 能手工启动注册任务、取消可取消 Run 并对失败或取消 Run 创建人工 retry。系统 MUST 在相同互斥键已有活动 Run 时返回现有 Run，不得创建重复 Run；人工 retry MUST 创建新 Run 并保留关联和审计。

#### Scenario: 管理员重复启动同一回填
- **WHEN** 管理员对已有活动互斥键再次点击启动
- **THEN** API 返回现有 Run，页面跳转或展示该 Run而不重复排队

### Requirement: 结构化事件和严格脱敏
Task Event MUST 包含 Run、事件类型、级别、时间、阶段、消息、可选错误码、受控 metadata、actor 和可选关联 ID。系统 MUST 禁止记录原始 stdout/stderr、Token、Cookie、密码、完整 SQL、完整请求体、响应体或未脱敏异常；metadata MUST 有大小上限并在截断时记录 `metadata_truncated`。

#### Scenario: provider 返回包含敏感头的错误
- **WHEN** 任务记录 provider 请求失败事件
- **THEN** 事件保留 provider、请求类型、状态码、attempt、耗时、错误码和脱敏摘要，但不包含认证头或完整响应体

### Requirement: 事件保留与运行摘要
Task Event MUST 默认保留 90 天并按受控批次清理；Task Run 的最终状态、通用结果摘要和领域引用 MUST 不随事件清理删除。结果详情 MUST 由领域接口提供，不得把无限制大 JSON 存入通用 Run。

#### Scenario: 清理九十天前事件
- **WHEN** 保留清理任务删除超过 90 天的 Task Event
- **THEN** Run 列表仍展示最终状态、结果摘要、错误码和领域详情入口

### Requirement: 查询、筛选与刷新契约
Schedules、Runs 和 Logs API MUST 提供稳定分页、排序及任务、状态、所有者、时间和错误码等适用筛选。第一版页面 MUST 每 5 秒轮询活动数据；事件查询 MUST 返回稳定 cursor，使未来可增加 SSE 而不改变事件顺序语义。

#### Scenario: 过滤 Eastmoney 回填错误
- **WHEN** 管理员按任务类型、provider 关联和失败状态筛选 Logs
- **THEN** 页面返回匹配事件并可通过关联 ID 打开对应 External Data Request Log

### Requirement: 管理操作审计
系统 MUST 记录计划创建、修改、暂停、启动 Run、取消、retry、执行模式切换和迁移操作的操作者、时间、目标、变更前后摘要及原因，并对普通用户隐藏无权查看的审计详情。

#### Scenario: 管理员修改计划参数
- **WHEN** 管理员保存通过 Schema 校验的新计划参数
- **THEN** 系统创建不可变审计记录并保留旧值和新值的脱敏摘要
