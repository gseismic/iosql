# PLAN-027-partitioned-sqlite-core-api

## 背景

上一版设计 `PLAN-026-table-api-redesign-reflection` 已经提出：主入口应从 `timeseries()` 转向原始 `Table`，`TableSpec` 退到高级/内部层。

用户进一步指出：不希望核心 API 必须出现 `time` 这个 key。分区字段可以是时间，也可以是序号、offset、block height、版本号或其他可排序字段。目标更接近：

```text
原生 SQLite 表 + 自动分区能力
```

而不是：

```text
时序数据库专用模型
```

本设计稿修正上一版结论：核心 API 不应强制 `time` 语义；`time` 只属于 `timeseries()` 等高层 convenience helper。

## 设计结论

核心抽象应命名为“分区表”而不是“时序表”：

- 核心层：`Database.table(...)` / `Table.insert_all()` / `Table.select()`，接近 SQLite。
- 自动分区层：通过 `partition_by` 或 `partition` 声明哪个字段用于分区，以及如何把字段值映射为分区 key。
- 可排序语义：通过 `order_by` 或 `order_key` 声明默认排序字段；它可以等于分区字段，也可以不同。
- 时序便利层：`Database.timeseries()` 保留，但只是 `partition_by=time column` 的封装。
- 高级契约层：内部仍需要持久化表契约，但契约名称不应围绕 `TimeColumn` 展开，而应围绕 `PartitionKey` / `OrderKey` / `PartitionPolicy`。

推荐主 API：

```python
from iosql import Database, Partition

db = Database("sqlite://examples/runtime/metrics.iosql")

metrics = db.table(
    "metrics",
    pk=["seq", "symbol"],
    columns={"seq": "integer", "symbol": "text", "value": "float"},
    partition=Partition.range("seq", every=1_000_000),
)

metrics.insert_all(rows)

rows = metrics.select(
    "*",
    where={"field": "seq", "op": "between_and", "value": [2_000_000, 2_100_000]},
    order_by="seq",
    to_dict=True,
)
```

时间只是这个模型的一个例子：

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    columns={"timestamp": "integer", "symbol": "text", "price": "float"},
    partition=Partition.duration("timestamp", every="1day"),
)
```

日历日期也是普通分区键：

```python
events = db.table(
    "events",
    pk=["event_date", "title"],
    columns={"event_date": "text", "title": "text", "room": "text"},
    partition=Partition.calendar("event_date", every="1day", timezone="UTC"),
)
```

## 为什么不应该强制 `time`

### 1. 分区的本质是可映射的 key，不是时间

系统真正需要知道的是：

1. 哪个字段参与分区。
2. 这个字段的值如何映射到分区 key。
3. 查询条件是否能推导出需要扫描哪些分区。

这三个问题不要求字段具有时间语义。

例如：

```python
blocks = db.table(
    "blocks",
    pk=["height"],
    partition=Partition.range("height", every=100_000),
)
```

```python
logs = db.table(
    "logs",
    pk=["offset"],
    partition=Partition.range("offset", every=10_000_000),
)
```

```python
versions = db.table(
    "snapshots",
    pk=["version", "object_id"],
    partition=Partition.range("version", every=1_000),
)
```

这些都不是 timeseries，但都需要自动分区。

### 2. `time` 会把用户引向错误心智

如果用户只是想要“SQLite 文件太大，帮我按字段拆分”，`time` 会让他误以为：

- 表必须是时间序列。
- 查询必须使用 `start/end/date`。
- 字段必须是时间戳或日期。
- 需要理解时区和时间单位。

这增加了不必要的认知负担。

### 3. 原生 SQLite 心智应该优先

SQLite 用户熟悉的是：

```python
table.insert_all(rows, pk=[...])
table.select("*", where=..., order_by=..., limit=...)
```

iosql 的核心增量应该是：

```python
partition=...
```

而不是新增一个时间序列领域模型。

## 新核心概念

### PartitionPolicy

替代当前文档层面的 `PartitionSpec.by_time(...)`。它描述“如何把某列的值映射到分区 key”。

建议用户可见构造器：

```python
Partition.range("seq", every=1_000_000)
Partition.duration("timestamp", every="1day")
Partition.calendar("event_date", every="1day", timezone="UTC")
Partition.hash("user_id", buckets=128)
Partition.value("tenant_id")
```

语义：

- `range`：任意可排序数值，按固定宽度分区。
- `duration`：时间戳数值，按毫秒/秒等持续时间分区；本质是带单位别名的 `range`。
- `calendar`：日期/时间字符串，按日/月/年等日历边界分区。
- `hash`：不可排序键的哈希桶分区；不能做范围裁剪，但能控制文件大小。
- `value`：按字段值直接分区，例如 tenant、country、category。

当前已有 `DurationPartition` / `CalendarPartition` 可以作为 runtime 实现，但用户入口建议统一到 `Partition.*` 工厂，避免 `DurationPartition(interval_ms="1day")` 这种参数名和字符串值不匹配的问题。

### partition_by

`partition_by` 是分区字段名，可以由 `partition` 推导，也可以显式传入。

推荐允许两种写法：

```python
db.table("metrics", pk=["seq"], partition_by="seq", partition_every=1_000_000)
```

和：

```python
db.table("metrics", pk=["seq"], partition=Partition.range("seq", every=1_000_000))
```

如果二者都传，必须一致。

### order_key / default_order_by

分区字段不一定等于排序字段。需要时可以声明默认排序：

```python
quotes = db.table(
    "quotes",
    pk=["trade_id"],
    partition=Partition.duration("received_at", every="1day"),
    order_key="exchange_seq",
)
```

核心系统不应把它叫 `time`。排序字段只是默认 `ORDER BY`、`latest` 类 helper 或布局统计的辅助元数据。

第一阶段可以不实现 `order_key`，但设计上应预留，避免再次把 `time` 塞回核心层。

### TableContract

内部仍需要契约对象，但建议从 `TableSpec(time=...)` 迁移为更通用的：

```python
TableContract(
    primary_key=["seq", "symbol"],
    columns={"seq": "integer", "symbol": "text", "value": "float"},
    partition=PartitionPolicy(kind="range", column="seq", every=1_000_000),
    order_key="seq",
    schema_policy="evolve",
    physical_layout=PhysicalLayout(...),
)
```

兼容当前代码时可以先保留 `TableSpec` 类名，但新增字段应向这个模型靠拢：

```python
TableSpec(
    primary_key=[...],
    partition=...,
    order_key=...,
    time=None,  # 兼容旧 timeseries helper，非核心字段
)
```

长期看，`TimeColumn` 应成为 timeseries/helper 层对象，而不是核心表契约必填概念。

## 推荐 API 形态

### 1. 建表：接近 SQLite，只多一个 partition

```python
db = Database("sqlite://data/app.iosql")

table = db.table(
    "metrics",
    pk=["seq", "symbol"],
    columns={
        "seq": "integer",
        "symbol": "text",
        "value": "float",
    },
    partition=Partition.range("seq", every=1_000_000),
)
```

也支持更短写法：

```python
table = db.table(
    "metrics",
    pk=["seq", "symbol"],
    partition_by="seq",
    partition_every=1_000_000,
)
```

规则：

- `pk` 是主参数名；`primary_key` 作为别名。
- `columns` 可选，继续支持 schema-on-write/evolve。
- `partition` 可选；不传就是普通单表/单文件布局。
- 表已存在时，新传契约必须兼容。

### 2. 写入：保留原生感

推荐：

```python
table.insert_all(rows)
```

或：

```python
table.write(rows)
```

两者可以共存，但语义要明确：

- `insert_all(rows)`：接近 SQLite，默认 append 或遵循 `conflict=`。
- `write(rows, mode="upsert")`：更高级，面向幂等写入。

如果目标是“更接近原生 SQLite”，文档主线应优先使用：

```python
table.insert_all(rows, pk=[...])
```

但为了避免每次重复传 `pk`，更推荐建表时声明：

```python
table = db.table("metrics", pk=["seq", "symbol"], partition=...)
table.insert_all(rows)
```

首次写入 bootstrap 可支持：

```python
db["metrics"].insert_all(
    rows,
    pk=["seq", "symbol"],
    partition=Partition.range("seq", every=1_000_000),
)
```

规则：只在表尚未有契约时根据参数创建契约；已有契约时必须兼容。

### 3. 查询：以 select/where 为核心

推荐主查询：

```python
rows = table.select(
    "*",
    where={"field": "seq", "op": "between_and", "value": [2_000_000, 2_100_000]},
    order_by="seq",
    limit=100,
    to_dict=True,
)
```

分区裁剪自动发生：

- 如果 `where` 中包含对分区字段的 `= / in / between / >= / <=`，系统推导候选分区。
- 如果 `where` 不涉及分区字段，则扫描所有分区。
- 用户不需要调用 `query(start/end)`。

解释计划：

```python
plan = table.explain_select(
    where={"field": "seq", "op": "between_and", "value": [2_000_000, 2_100_000]},
)
```

或兼容当前链式：

```python
plan = table.select_query("*", where=...).explain()
```

但不建议核心 API 以 `query(start/end/date)` 为主，因为那重新引入时间序列心智。

### 4. 时序 helper：保留但下沉

保留：

```python
ticks = db.timeseries(
    "ticks",
    time="timestamp",
    pk=["timestamp", "symbol"],
    partition_every="1day",
)
```

它等价于：

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    partition=Partition.duration("timestamp", every="1day"),
    order_key="timestamp",
)
```

`timeseries()` 可以继续提供：

- `query(start=..., end=...)`
- `latest()`
- `asof()`
- `date=...`
- `session=...`

但这些是 helper，不是分区表核心。

## 与当前代码的映射

当前代码已经有：

- `DurationPartition`
- `CalendarPartition`
- `Table.partition`
- `insert_all(..., pk=..., partition=...)`
- `select(fields, where=...)`
- `TableSpec`
- `Table.write()`
- `Table.query(start/end/date/...)`

这说明“接近 SQLite 的自动分区核心”不是全新方向，而是旧能力和新能力的重新分层。

映射建议：

| 新设计概念 | 当前代码对应物 | 处理方式 |
| --- | --- | --- |
| `Partition.range(...)` | 暂无通用名称；`DurationPartition` 可覆盖数值 range | 新增工厂，内部可复用/重命名 runtime 类 |
| `Partition.duration(...)` | `DurationPartition` | 新增更自然工厂 |
| `Partition.calendar(...)` | `CalendarPartition` | 新增更自然工厂 |
| `partition_by` | `Partition.ts_col` / `PartitionSpec.column` | 统一命名为 partition column |
| `TableContract` | `TableSpec` | 先扩展，后续可重命名 |
| `select(...).explain` | 当前 `query().explain()` | 增加 select explain 能力 |
| `timeseries()` | 当前主 helper | 保留为 helper |

## 对上一版 PLAN-026 的修正

`PLAN-026` 中这类推荐不再作为最终方向：

```python
db.table(
    "metrics",
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
)
```

修正为：

```python
db.table(
    "metrics",
    pk=["timestamp", "symbol"],
    partition=Partition.duration("timestamp", every="1day"),
)
```

或：

```python
db.table(
    "metrics",
    pk=["seq", "symbol"],
    partition=Partition.range("seq", every=1_000_000),
)
```

核心变化：

- 删除核心 API 中的 `time=`。
- `partition` 自己携带字段名和映射策略。
- 可选 `order_key` 表达默认排序，不与分区或时间绑定。
- `query(start/end/date)` 不再是核心主查询，`select(where=...)` 才是。

## 实施建议

### 阶段 1：新增别名和工厂，不破坏现有 API

新增：

```python
from iosql import Partition

Partition.range(column, every)
Partition.duration(column, every, unit="ms")
Partition.calendar(column, every="1day", timezone="UTC")
```

新增 `Database.table()` 轻量参数：

```python
Database.table(
    table_name,
    pk=None,
    primary_key=None,
    columns=None,
    partition=None,
    partition_by=None,
    partition_every=None,
    order_key=None,
    spec=None,
)
```

内部生成现有 `TableSpec`，但不要求用户传 `time`。

### 阶段 2：让 `select()` 具备一等 explain

当前 `select()` 已可根据 `where` 做分区读取，但面向用户的 explain 主要在 `query()`。应增加：

```python
table.explain_select(fields="*", where=...)
```

或：

```python
table.select_query("*", where=...).explain()
```

这样用户不需要进入 timeseries `query(start/end)` 才能查看裁剪计划。

### 阶段 3：文档主线切换

README 主线改为：

1. 创建分区 SQLite 表。
2. `insert_all()` 写入。
3. `select()` 查询并自动裁剪。
4. `explain_select()` 查看分区扫描计划。
5. `timeseries()` 作为可选 helper。
6. `TableSpec` 作为高级契约对象。

示例命名也应避免第一屏全部是 `timeseries`：

- `01_partitioned_table_range.py`
- `02_partitioned_table_calendar.py`
- `03_timeseries_helper.py`
- `04_layout_report_and_repartition.py`

### 阶段 4：逐步降低 `TimeColumn` 的核心地位

兼容保留：

- `TableSpec.time`
- `TimeColumn`
- `Database.timeseries()`
- `Table.query(start/end/date)`

新代码优先：

- `TableSpec.partition.column`
- `order_key`
- `select(where=...)`
- `Partition.*` 工厂

如果后续需要重构内部模型，可以引入：

```python
PartitionedTableSpec
```

或直接让 `TableSpec` 增加：

```python
partition_key: Optional[str]
order_key: Optional[str]
```

而 `time` 只作为兼容字段。

## 关键设计规则

1. 核心 API 不出现必填 `time`。
2. 自动分区只要求字段可映射到分区 key，不要求字段是时间。
3. 分区裁剪基于 `where` 和分区策略，不基于 `query(start/end)`。
4. `timeseries()` 是 helper，不是核心。
5. `TableSpec` 是内部/高级契约，不应成为入门门槛。
6. `insert_all/select` 保留原生 SQLite 体感。
7. 已存在表的契约必须持久化和兼容校验，不能回到每次写入任意传不同 pk/partition。

## 推荐最终用户体验

### 数值序号分区

```python
db = Database("sqlite://data/app.iosql")

table = db.table(
    "events",
    pk=["seq"],
    partition=Partition.range("seq", every=1_000_000),
)

table.insert_all(rows)

rows = table.select(
    "*",
    where={"field": "seq", "op": "between_and", "value": [1_000_000, 1_100_000]},
    to_dict=True,
)
```

### 时间戳分区，但不要求 `time=`

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    partition=Partition.duration("timestamp", every="1day"),
)

ticks.insert_all(rows)

rows = ticks.select(
    "*",
    where={"field": "timestamp", "op": "between_and", "value": [start_ms, end_ms]},
    order_by="timestamp",
    to_dict=True,
)
```

### 日期字符串分区

```python
schedule = db.table(
    "schedule",
    pk=["event_date", "title"],
    partition=Partition.calendar("event_date", every="1day", timezone="UTC"),
)

schedule.insert_all(events)

rows = schedule.select(
    "*",
    where={"field": "event_date", "op": "==", "value": "2025-01-03"},
    to_dict=True,
)
```

### timeseries helper 仍可用

```python
ticks = db.timeseries(
    "ticks",
    time="timestamp",
    pk=["timestamp", "symbol"],
    partition_every="1day",
)

rows = ticks.query(start=start_ms, end=end_ms).to_list()
```

这个 helper 是方便写法，不改变核心定位。

## 结论

如果目标是“更接近原生 SQLite，仅增加自动分区”，那么核心 API 应从：

```python
time + query(start/end)
```

调整为：

```python
partition + select(where)
```

核心用户不需要知道 `TimeColumn`。他们只需要知道：这是一张 SQLite 表，主键是什么，按哪个字段怎么分区，查询仍然写 where。系统负责根据 where 自动裁剪分区。

`time` 这个概念仍有价值，但只应存在于 `timeseries()` helper 和 `latest/asof/date/session` 这类时序便利能力中，不应成为分区表核心抽象。
