---
name: iosql-usage
description: 使用 iosql 构建、审查或调试本地 SQLite 时序数据存储。Use when working with iosql APIs such as Database, table, TableSpec, timeseries, write, query, latest, asof, partitions, segments, layout_report, repartition_to, or iosql.utils.mysqlite; also use when designing financial time-series schemas, partition strategies, ingestion workflows, or tests for this repository.
---

# iosql Usage

## 核心判断

优先使用主 API：

```python
from iosql import Database
```

只有在明确需要直接操作单个 SQLite 文件、schema-on-write、动态列或连接池细节时，才使用低层模块：

```python
from iosql.utils.mysqlite import Database
```

## 工作流

1. 明确数据模型：时间列、主键、列类型、自然日还是交易日/session、预期单日或单分区数据量。
2. 声明表契约有两个等价入口：显式版用 `TableSpec` 并传给 `Database.table(name, spec=spec)`；快捷版用 `Database.timeseries()` 自动生成同等 `TableSpec`。
3. 对毫秒时间戳使用 duration 分区；对 `YYYY-MM-DD` 日期字符串使用 `TimeColumn(unit="date")` + calendar 分区，或在 `timeseries()` 中传 `time_unit="date"` 和 `partition_kind="calendar"`。
4. 写入使用 `table.write()` 或 `table.write_buffer()`。按主键覆盖用默认 `mode="upsert"`，严格追加用 `mode="append"`，忽略重复用 `mode="ignore"`。
5. 查询使用 `table.query(...).to_list()`；需要确认分区裁剪时调用 `.explain()`。
6. 金融截面查询使用 `latest()` 或 `asof()`，多维截面传 `by=["symbol", "timeframe"]` 等。
7. 高频数据关注 `segments()` 和 `layout_report()`；改变分区布局时使用 `repartition_to()` 写入新库，不要原地改旧库。
8. 完成后关闭 `db.close()`，测试中优先用 `tmp_path` 和独立 `sqlite://...` URI。

## 引用资料

需要代码模板、常见表设计和测试建议时，读取 [usage-patterns.md](references/usage-patterns.md)。

仓库内也可参考：

- `README.md`
- `examples/01_quickstart_table.py`
- `examples/01_quickstart_timeseries.py`
- `examples/02_calendar_events_table.py`
- `examples/05_partition_segments_smoke.py`
- `examples/finance/`
- `tests/sqlite/test_timeseries_contract.py`

## 设计偏好

- 让 `TableSpec` 成为表契约来源，避免隐式依赖第一次写入推断出的结构。
- 查询 API 优先表达业务条件：`symbols/start/end/date/session/where/order_by/limit`。
- 复杂 where 优先使用受支持的 text where 或 `Case`，不要拼接原始 SQL。
- 测试必须覆盖写入结果、查询结果和必要的 `explain()` 裁剪信息。
- 修改核心行为时，至少跑相关 sqlite 契约测试、partition 测试和 mysqlite 测试。
