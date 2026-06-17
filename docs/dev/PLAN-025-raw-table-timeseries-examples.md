# PLAN-025-raw-table-timeseries-examples

## 背景

当前 `examples/` 中大量示例使用 `Database.timeseries()`。用户希望确认当前代码是否支持直接通过原始 `Table` 完成时间序列数据写入、读取和分区，并希望新增不用 `timeseries()` 的示例，降低用户对专用入口的认知负担，同时保留 `timeseries()`。

## 当前判断

- `Database.table(name, spec=TableSpec(...))` 已经是公开入口。
- `Table.write()`、`Table.query()`、`Table.latest()`、`Table.layout_report()` 和 `Table.repartition_to()` 都基于 `TableSpec` 工作。
- `Database.timeseries()` 本质上是 `TableSpec.timeseries(...)` 的便捷封装，原始 `Table` 入口理论上可以表达同样的主键、时间列、分区和物理布局能力。

## 计划

1. 补充契约测试，证明不用 `Database.timeseries()`，只用 `Database.table(..., spec=TableSpec(...))` 可以：
   - 写入毫秒时间戳时间序列数据。
   - 按 symbol 和时间窗口读取数据。
   - 生成并裁剪 duration 分区。
   - close 后重新打开仍能读取持久化 `TableSpec`。
2. 保留原有 `timeseries()` 入门示例，新增并列的原始 `Table` + `TableSpec` 示例，展示完整写入、读取、分区计划流程。
3. 调整 examples 索引和 README，使入口从“只有 timeseries 示例”变为“双版本示例”：原始 `Table` 显式版和 `timeseries()` 快捷版都可运行。
4. 运行相关测试和新增示例。
5. 生成 OUTCOME 文档并提交中文 commit，提交信息记录计划和结果文件。

## 风险

- `TableSpec(...)` 写法比 `Database.timeseries()` 更显式，示例需要控制信息密度，并通过并列版本降低迁移成本。
- 如果发现当前 API 缺少语法糖，本计划先以可运行示例证明能力，不贸然修改核心 API。
