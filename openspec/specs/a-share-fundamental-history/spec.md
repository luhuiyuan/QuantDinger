# a-share-fundamental-history Specification

## Purpose
TBD - created by archiving change build-a-share-fundamental-history. Update Purpose after archive.
## Requirements
### Requirement: 年度基本面历史与版本留存
系统 MUST 为沪深普通股（包括退市股票）按报告期、来源、公告可得日期和内容版本追加保存年度原始基本面观察、结构化证据、来源标识、公告引用与内容哈希；修订不得覆盖旧版本。

#### Scenario: 供应商修订已披露年报
- **WHEN** 同一股票、报告期和来源返回不同内容哈希
- **THEN** 系统保留旧观察并创建带新版本和证据的新观察

### Requirement: Point-in-time 年度查询
系统 SHALL 仅向给定 as-of 日期暴露公告日不晚于该日期的最新有效观察，并保留该观察的来源和版本。

#### Scenario: 报告尚未公告
- **WHEN** 报告期已结束但公告日晚于查询 as-of 日期
- **THEN** 查询不得返回该报告或由它计算的指标

### Requirement: 标准指标由原始科目计算
系统 MUST 使用版本化公式从原始年度科目计算质量指标，不得以数据商已算比率直接决定结果；原始比率仅作为核验信息。

#### Scenario: 计算五年 FCF
- **WHEN** 最近五个完整年度均有经营现金流和购建长期资产现金支出
- **THEN** 系统以每年经营现金流减该现金支出后求和并返回公式版本

### Requirement: 缺失科目与行业边界
系统 MUST 将缺失必要科目、无效分母或不足规则窗口标记为数据不足；非银行、非保险公司缺少独立利息费用时不得以财务费用代替。当前查询范围必须排除北交所与非普通股。

#### Scenario: 缺少利息费用
- **WHEN** 非豁免股票无法映射年度独立利息费用
- **THEN** 利息覆盖指标状态为数据不足而非通过或代理计算
