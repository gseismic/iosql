---
name: iosql-usage
description: 使用 iosql 构建、审查或调试本地 SQLite 时序数据存储。Use when working with iosql APIs such as Database, table, TableSpec/TableContract, timeseries, Partition (duration/calendar/range), Segment, write, write_buffer, query, latest, asof, select, explain_select, segments, layout_report, repartition_to, spec, table_parts, delete, truncate, create_index, Case, CaseExpression, Where, WriteResult, QueryPlan, LayoutReport, or iosql.utils.mysqlite; also use when designing financial time-series schemas, partition strategies, ingestion workflows, layout maintenance, or tests for this repository.
---

# iosql 使用指南

## 核心判断

优先使用主 API：

```python
from iosql import Database
```

只有在明确需要直接操作单个 SQLite 文件、schema-on-write、动态列或连接池细节时，才使用低层模块：

```python
from iosql.utils.mysqlite import Database
```

## 决策树：选对入口

```
需要逻辑分区 + 物理 segment？
├─ 是 → Database.table(..., pk=..., partition=..., segment=..., order_key=...)
│      适用于：任意可排序字段分区（时间戳、序号、offset 等）
│
需要 timeseries 便利查询（start/end/date/latest/asof）？
├─ 是 → Database.timeseries(...)
│      适用于：K线、tick、orderbook、新闻等带时间的金融数据
│
只需要纯 row segment 滚动（按行数拆文件）？
├─ 是 → Database.table(..., segment=Segment.rows(max_rows=N))
│      适合日志、事件等不需要逻辑分区、只需控制文件大小的场景
│
需要直接操作单个 SQLite 文件？
└─ 是 → iosql.utils.mysqlite.Database
       适合：独立 SQLite 工具、schema-on-write 快速原型
```

## Partition 与 Segment 的关系

`partition`（逻辑分区）和 `segment`（物理分段）是独立的两级机制，组合使用形成不同存储模式：

```
Partition（逻辑分区）           Segment（物理分段）
按业务键路由到不同目录         在同一逻辑分区内按行数拆分 .sqlite3 文件
e.g. ts=1735689600000          e.g. max_rows=500_000
```

### 四种组合及行为

| Partition | Segment | 行为 | 目录结构 |
|:---------:|:-------:|------|---------|
| ✅ | ✅ | 按分区键路由，每个分区内按行数自动滚动文件 | `partition_key.sqlite3` / `partition_key-1.sqlite3` |
| ✅ | ❌ | 按分区键路由，每个分区仅一个文件（无限增长） | `partition_key.sqlite3` |
| ❌ | ✅ | 全部数据写入 `__all__`，仅按行数滚动文件 | `__all__-0.sqlite3` / `__all__-1.sqlite3` |
| ❌ | ❌ | 退化为单文件 SQLite | `table_name.sqlite3` |

### 查询裁剪

有 `segment` 时执行**二级裁剪**：

1. **一级**：按 `partition` 键跳过不相关的逻辑分区
2. **二级**：通过 segment `stats`（每列 min/max）和 `session_min/session_max` 跳过不相关的物理文件

`explain_select()` 返回的 `QueryPlan` 同时展示 `scanned_partitions` / `scanned_segments` 和对应的 `skip_reasons`。

### 核心代码逻辑

```python
# do_insert_all 中的关键决策
part_obj    = partition or self._partition      # 有逻辑分区？
use_segment = bool(spec and spec.segment)       # 有物理分段？

if part_obj is None:
    logical_part = DEFAULT_LOGICAL_PART if use_segment else None  # "__all__" or None
else:
    partitioned_data = {part_obj.get_key(row): [rows...]}         # 按分区键拆散

for part_key, part_data in partitioned_data.items():
    if use_segment:
        _do_insert_segmented(...)   # segment 感知：检查行数、seal、滚动
    else:
        _do_insert_part(...)        # 直接写入对应 SQLite 文件
```

## API 速查表

| 类别 | API | 说明 |
|------|-----|------|
| **入口** | `Database(uri)` | 创建/打开数据库，`sqlite://` 或 `postgresql://` URI |
| | `Database.table(name, **opts)` | 创建/获取表（核心入口） |
| | `Database.timeseries(name, time=..., primary_key=..., **opts)` | 时序 helper 快捷版 |
| **写入** | `table.write(rows, mode=None)` | 写入行；默认使用表契约 `conflict`，新表默认 append |
| | `table.write_buffer(mode=None, max_rows=...)` | 缓冲批量写入（上下文管理器） |
| | `table.insert_all(rows, ...)` | 低层写入（需明确传 pk/partition） |
| | `table.upsert_all(kv_list, pk)` | 按主键合并写入（merge 字段） |
| | `table.delete(where)` | 条件删除（传 None 已废弃，改用 truncate） |
| | `table.truncate(confirm=True)` | 清空表（需显式确认） |
| **查询** | `table.query(...).to_list()` / `.explain()` | 时序高层查询（timeseries 表） |
| | `table.select(fields, where, order_by, limit, to_dict)` | 底层条件查询（所有表） |
| | `table.select_all(fields, where)` | 等价于 select(to_dict=True) |
| | `table.explain_select(fields, where, order_by, limit)` | 查询计划/分区裁剪分析 |
| | `table.latest(symbols, as_of, by, columns)` | 最新截面查询 |
| | `table.asof(...)` | latest 的别名 |
| **诊断** | `table.spec()` | 获取当前表契约 |
| | `table.segments()` | 获取物理 segment 清单 |
| | `table.table_parts()` | 获取逻辑分区列表 |
| | `table.layout_report()` | 分区诊断报告（行数、推荐） |
| | `table.repartition_to(target_uri, spec)` | 按新策略重分区到新库 |
| **维护** | `table.create_index(column, order)` | 为所有分区创建索引 |
| | `Database.table_names()` | 列出数据库中所有表 |
| | `db.close()` | 关闭所有连接 |

## 工作流

### 1. 明确数据模型

确定时间列、主键、列类型、自然日还是交易日/session、预期单日或单分区数据量。

### 2. 声明表契约

两种入口等价：

**显式版（推荐）**：
```python
from iosql import Database, Partition, Segment
db = Database("sqlite://db/market.iosql")
ticks = db.table(
    "ticks",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition=Partition.duration("ts", every="1day"),
    segment=Segment.rows(max_rows=500_000),
    order_key="ts",
)
```

**快捷版（timeseries helper）**：
```python
ticks = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1day",
)
```

### 3. 选择分区策略

| 分区类型 | API | 适用场景 | 分区键格式 |
|---------|-----|---------|-----------|
| duration | `Partition.duration("ts", every="1hour")` | 毫秒时间戳 | 整数（对齐边界） |
| calendar | `Partition.calendar("date", every="1day", timezone="Asia/Shanghai")` | 日期字符串 | `YYYYMMDD` 等 |
| range | `Partition.range("seq", every=10000)` | 任意可排序数值 | 整数（对齐边界） |

### 4. 写入

```python
# 单条/多条写入
result = table.write([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
])

# 缓冲批量写入（推荐大量数据时使用）
with table.write_buffer(max_rows=10_000) as buf:
    for batch in data_source:
        buf.append(batch)
# 退出上下文时自动 flush

# 写入模式
# mode=None：使用表契约 conflict，新表默认 append
# mode="append"：严格追加，重复主键通常由 SQLite 约束处理
# mode="ignore"：重复主键跳过
# mode="upsert"/"replace"：覆盖写；当前不支持 Segment.rows 表
```

### 5. 查询

**时序查询（timeseries 表）**：
```python
# 时间窗口查询
rows = ticks.query(start=..., end=..., symbols="AAPL").to_list()

# 查询计划（分区裁剪分析）
plan = ticks.query(start=..., end=...).explain()
print(plan.scanned_partitions, plan.skipped_partitions)
```

**底层条件查询（所有表）**：
```python
where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between_and", "value": [start, end]},
]
rows = ticks.select("*", where=where, order_by="ts", limit=100, to_dict=True)
plan = ticks.explain_select("*", where=where, order_by="ts")
```

**金融截面查询**：
```python
# 最新值（每个 symbol 截至 as_of 的最新行）
latest = ticks.latest(
    symbols=["AAPL", "MSFT"],
    as_of=1735776000000,
    by=["symbol", "timeframe"],
    columns=["symbol", "timeframe", "ts", "price"],
).to_list()

# 单条获取
row = ticks.latest(symbols="AAPL", as_of=ts).one()
```

### 6. 交易日 / Session

期货夜盘、跨时区行情使用 `session_column`：

```python
futures = db.timeseries(
    "futures_ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "trading_date": "text", "price": "float"},
    session_column="trading_date",
)
rows = futures.query(session="2026-01-06", symbols="AU").to_list()
```

### 7. 布局诊断和重分区

```python
report = table.layout_report()
print(report.total_rows, report.recommendation)

new_spec = table.spec().with_partition(every="1hour")
result = table.repartition_to("sqlite://db/hourly.iosql", spec=new_spec)
print(result.rows_copied, result.partitions_created)
```

### 8. 清理

```python
# 关闭（推荐）
db.close()

# 上下文管理器（自动关闭）
with Database("sqlite://db/test.iosql") as db:
    ...
```

## 设计偏好

- 让 `TableSpec/TableContract` 成为表契约来源，避免隐式依赖第一次写入推断出的结构。
- 查询 API 优先表达业务条件：`symbols/start/end/date/session/where/order_by/limit`。
- 复杂 where 优先使用受支持的 dict/list/`Case` 或文本 where，不要拼接原始 SQL。
- 代码结构按层理解：`iosql.core.query` 是通用查询 AST/where 解析；`iosql.engines.sqlite` / `iosql.engines.postgresql` 是引擎实现；旧 `iosql.query` / `iosql.sqlite` / `iosql.postgresql` 包已删除。
- `segment` 支持 `max_rows`/`target_rows`/`min_rows` 三级参数，合理配置以控制文件大小。
- `repartition_to()` 写入新库，不要假设它会原地修改源库。
- 测试必须覆盖写入结果、查询结果和必要的 `explain()` 裁剪信息。
- 修改核心行为时，至少跑相关 sqlite 契约测试、partition 测试和 mysqlite 测试。

## 反模式（避免）

- ❌ 直接传文件路径给主 API（如 `Database("db/market.iosql")`）→ 必须用 `sqlite://` 前缀
- ❌ 对已存在表重复声明不同契约 → 用 `db.table("name")` 获取引用而非重新声明
- ❌ 在写入前未声明 `primary_key` → `write()` 必须有 pk
- ❌ 用 `delete(None)` → 已废弃，改用 `truncate(confirm=True)`
- ❌ 假设 `repartition_to()` 会修改原库 → 它只写新库
- ❌ 拼接原始 SQL 做 where → 用 dict/list/`Case`/文本表达式
- ❌ 忽略 `WriteResult` 返回值 → 检查 `rows_seen` vs `rows_written`
- ❌ 忽略测试数据隔离 → 每个测试用独立 `tmp_path`
- ❌ 用 `insert_all()` 替代 `write()` → `insert_all` 是低层 API，不自动使用表契约

## 引用资料

需要代码模板时，读取 [usage-patterns.md](references/usage-patterns.md)。

遇到错误时，读取 [troubleshooting.md](references/troubleshooting.md)。

需要完整 API 签名时，读取 [api-reference.md](references/api-reference.md)。

仓库内也可参考：

- `README.md`
- `examples/01_quickstart_table.py`
- `examples/01_quickstart_timeseries.py`
- `examples/02_calendar_events_table.py`
- `examples/02_calendar_events.py`
- `examples/03_buffered_ingest_and_query_plan.py`
- `examples/04_layout_report_and_repartition.py`
- `examples/05_partition_segments_smoke.py`
- `examples/06_row_segment_late_data.py`
- `examples/finance/`
- `tests/engines/sqlite/test_timeseries_contract.py`
