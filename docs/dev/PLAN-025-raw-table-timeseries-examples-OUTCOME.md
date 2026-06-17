# PLAN-025-raw-table-timeseries-examples-OUTCOME

## 完成内容

- 确认当前代码已经支持不用 `Database.timeseries()`，直接通过原始 `Table` 写入、读取和分区时间序列数据：
  - 入口为 `Database.table(name, spec=TableSpec(...))`。
  - `Table.write()` 使用 `TableSpec.primary_key`、`TableSpec.time` 和 `TableSpec.partition` 写入并分区。
  - `Table.query(start/end/date/symbols)` 使用 `TableSpec.time` 解释时间条件，并结合 `TableSpec.partition` 做分区裁剪。
  - `TableSpec` 会持久化到表元数据，关闭后重新打开仍能继续查询。
- 保留原有 `timeseries()` 示例，并新增并列的原始 `Table` 示例：
  - `examples/01_quickstart_timeseries.py`：保留 `Database.timeseries()` 毫秒时间戳快捷版。
  - `examples/01_quickstart_table.py`：新增原始 `Table` + `TableSpec` 毫秒时间戳显式版。
  - `examples/02_calendar_events.py`：保留 `Database.timeseries()` 日期字符串和 calendar 分区快捷版。
  - `examples/02_calendar_events_table.py`：新增原始 `Table` + `TableSpec` 日期字符串和 calendar 分区显式版。
- 补充测试 `test_raw_table_spec_writes_queries_and_partitions_timeseries`，覆盖：
  - 原始 `Table` 写入。
  - duration 分区创建和 `partitions_touched`。
  - 按 symbol + 时间窗口读取。
  - `query().explain()` 分区裁剪。
  - close 后重新打开并读取持久化 `TableSpec`。
- 更新 README 和 examples 索引，把文档从单一 `timeseries()` 入口调整为双入口：
  - 显式版：`Database.table(..., spec=TableSpec(...))`。
  - 快捷版：`Database.timeseries()`。
- 更新 `skills/iosql-usage` 和低层 mysqlite README，避免继续暗示 `timeseries()` 是唯一或必须的主入口。
- 在 README 和 raw Table 示例中说明 `TableSpec.time` 与 `TableSpec.partition` 的分工：
  - `time` 是业务时间语义，用于 `start/end/date/latest/asof`。
  - `partition` 是物理布局，用于写入落盘和查询裁剪。

## Review 结果

- 当前核心 API 不需要修改即可支持用户目标；`Database.timeseries()` 本质上是生成 `TableSpec` 的快捷入口。
- 原始 `Table` 示例没有使用 `Database.timeseries()` 或 `TableSpec.timeseries()`，避免用户继续绑定到 timeseries 专用命名。
- 保留 timeseries 示例作为快捷版，满足两个版本并存的要求。
- `TableSpec.time` 看似与 `partition` 重复，但职责不同；删除会让时间条件、日期时区归一化和 `latest/asof` 失去明确语义。

## 验证

- `python -m pytest tests/sqlite/test_timeseries_contract.py -q`
  - 结果：`36 passed`
  - 说明：仅有 pytest cache 写入上级只读目录警告，不影响测试结果。
- `python examples/01_quickstart_table.py`
  - 结果：通过，输出 3 行写入、2 个 duration 分区、AAPL 查询结果。
- `python examples/01_quickstart_timeseries.py`
  - 结果：通过，保留快捷版输出。
- `python examples/02_calendar_events_table.py`
  - 结果：通过，输出 `20260105`、`20260106` 两个 calendar 分区和查询结果。
- `python examples/02_calendar_events.py`
  - 结果：通过，保留快捷版输出。
- `python -m pytest tests -q`
  - 结果：`163 passed, 14 skipped`
  - 说明：仅有 pytest cache 写入上级只读目录警告，不影响测试结果。
- `python -m compileall -q iosql examples skills tests/sqlite/test_timeseries_contract.py`
  - 结果：通过。
- `git diff --check`
  - 结果：通过。

## 结论

当前代码支持通过原始 `Table` 完成时间序列写入、读取和分区。本次已补充并验证不用 `timeseries()` 的示例，同时保留原有 `timeseries()` 快捷版示例。
