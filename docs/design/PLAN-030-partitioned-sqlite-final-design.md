# PLAN-030-partitioned-sqlite-final-design

## 背景

前序设计稿已经讨论过三个问题：

- `time` 不应出现在核心 `Table` API 中。
- iosql 的核心定位应更接近“原生 SQLite + 自动分区”，而不是时序数据库专用模型。
- 当单位时间或单位分区的数据密度不确定时，需要支持按行数滚动的物理分段。

本稿是最终收敛版本。后续实现以本设计为准，可以破坏旧 API，不需要为了兼容保留废代码。

历史设计稿定位：

- `PLAN-026-table-api-redesign-reflection.md`：接口反思记录。
- `PLAN-027-partitioned-sqlite-core-api.md`：去 `time` 化的核心 API 草案。
- `PLAN-028-row-count-segments-and-wal.md`：按行数分段、晚到数据、查询影响草案。
- 本文档：最终设计与实现取舍。

## 一句话目标

iosql 应提供：

```text
接近原生 SQLite 的 Table API + 自动逻辑分区 + 自动物理分段 + 可解释查询计划
```

不应提供：

```text
以 time 为核心的特殊 Table 模型
```

时间序列只是普通分区表的一种用法。

## 最终结论

### 1. 核心 API 不再有 `time`

核心建表只保留 SQLite 风格概念：

```python
table = db.table(
    "metrics",
    pk=["seq", "symbol"],
    columns={"seq": "integer", "symbol": "text", "value": "real"},
    partition=Partition.range("seq", every=1_000_000),
    segment=Segment.rows(max_rows=500_000),
)
```

`time` 不应是 `TableContract`、`TableSpec` 或 `Table` 的核心字段。

如果需要时序快捷接口，可以保留：

```python
ticks = db.timeseries(
    "ticks",
    ts="timestamp",
    pk=["timestamp", "symbol"],
    every="1day",
)
```

但它必须只是下面写法的薄封装：

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    columns={"timestamp": "integer", "symbol": "text", "price": "real"},
    partition=Partition.duration("timestamp", every="1day"),
    order_key="timestamp",
)
```

`timeseries()` 可以保留，因为它有业务价值；但旧的以 `TimeColumn` 驱动的核心模型应删除或下沉为 helper 私有逻辑。

### 2. 分区字段可以是任意可排序字段

分区字段可以是：

- 时间戳：`timestamp`
- 日期字符串：`event_date`
- 自增序号：`seq`
- 消息 offset：`offset`
- 区块高度：`height`
- 版本号：`version`
- 租户或类别：`tenant_id`

系统只关心：

1. 哪个字段用于路由。
2. 如何把字段值映射为逻辑分区 key。
3. 查询条件能否推导出候选逻辑分区。

这些都不要求字段有时间语义。

### 3. 区分逻辑分区和物理分段

最终模型必须拆成两个正交维度：

```text
Table
  -> optional PartitionPolicy  逻辑路由
  -> optional SegmentPolicy    物理滚动
```

`Partition` 负责：

- 根据行内容决定 logical partition。
- 根据 where 条件裁剪 logical partitions。

`Segment` 负责：

- 控制 SQLite 文件大小。
- 决定 active segment 何时 seal。
- 维护 segment manifest 和列级统计。

一个逻辑分区可以包含多个物理 segment：

```text
logical partition: 2026-01-01
  segment 000000.sqlite3
  segment 000001.sqlite3
  segment 000002.sqlite3
  segment 000003.delta.sqlite3
```

逻辑分区不是文件。物理 segment 才是 SQLite 文件。

### 4. 按行数分区不是 `Partition`

按行数滚动不应叫 `Partition.rows(...)` 作为核心模型，因为一行数据不能单独算出自己属于第几个 row partition。

按字段分区是确定性的：

```python
Partition.range("seq", every=1_000_000)
```

按行数滚动是写入状态相关的：

```text
当前 active segment 已有多少行？
写完这一批是否超过 max_rows？
是否需要创建下一个 segment？
```

所以最终 API 使用：

```python
Segment.rows(max_rows=500_000)
```

可以在文档中解释为“按行数分段”，但实现上不应混进 `PartitionPolicy`。

## 最终用户 API

### 普通未分区表

```python
db = Database("sqlite://data/app.iosql")

items = db.table(
    "items",
    pk=["id"],
    columns={"id": "integer", "name": "text"},
)

items.insert_all([{"id": 1, "name": "alpha"}])
rows = items.select("*", where={"field": "id", "op": "==", "value": 1})
```

这应该尽量接近用户对 SQLite 的直觉。

### 按任意数值范围分区

```python
metrics = db.table(
    "metrics",
    pk=["seq", "symbol"],
    columns={"seq": "integer", "symbol": "text", "value": "real"},
    partition=Partition.range("seq", every=1_000_000),
)

metrics.insert_all(rows)

rows = metrics.select(
    "*",
    where={"field": "seq", "op": "between_and", "value": [2_000_000, 2_100_000]},
    order_by="seq",
)
```

### 按日历字段分区

```python
schedule = db.table(
    "schedule",
    pk=["event_date", "title"],
    columns={"event_date": "text", "title": "text", "room": "text"},
    partition=Partition.calendar("event_date", every="1day", timezone="UTC"),
)
```

### 按时间戳分区

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    columns={"timestamp": "integer", "symbol": "text", "price": "real"},
    partition=Partition.duration("timestamp", every="1day"),
    order_key="timestamp",
)
```

这里的 `timestamp` 只是分区字段，不是核心 API 的特殊 `time` 字段。

### 仅按行数物理分段

```python
events = db.table(
    "events",
    pk=["event_id"],
    columns={"event_id": "integer", "seq": "integer", "kind": "text"},
    segment=Segment.rows(max_rows=500_000),
)
```

适合：

- 数据密度不可预测。
- 主要目标是控制单个 SQLite 文件大小。
- 查询条件可能覆盖多个业务字段。
- 不想提前选择时间或范围分区。

内部只有一个默认逻辑分区：

```text
logical_part = "__all__"
```

### 逻辑分区 + 行数物理分段

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    columns={"timestamp": "integer", "symbol": "text", "price": "real"},
    partition=Partition.duration("timestamp", every="1day"),
    segment=Segment.rows(max_rows=500_000),
    order_key="timestamp",
)
```

适合：

- 希望先按日期、租户、范围等逻辑维度缩小扫描空间。
- 又担心单个逻辑分区数据过大。
- 单位时间数据密度不确定。

这是推荐的高容量默认形态。

## 公开概念

## 代码分层

实现层按职责分为三类包：

```text
iosql.core.query        通用查询 AST、where parser、Case/Where 表达式
iosql.engines.sqlite   SQLite 引擎实现
iosql.engines.postgresql PostgreSQL 引擎实现
```

旧入口只作为兼容转发层：

```text
iosql.query       -> iosql.core.query
iosql.sqlite      -> iosql.engines.sqlite
iosql.postgresql  -> iosql.engines.postgresql
```

后续新增通用查询能力时应进入 `iosql.core.query`，不要放入某个具体引擎目录；新增具体 SQL 编译或执行细节时应进入对应 `iosql.engines.*`。

### Database

```python
db = Database("sqlite://data/app.iosql")
```

职责：

- 管理 manifest。
- 打开 Table。
- 管理连接生命周期。

### Table

```python
table = db.table("name", ...)
```

职责：

- 写入：`insert_all` / `update` / `delete`。
- 查询：`select` / `explain_select`。
- 布局维护：`segments` / `compact` / `stats`。

### TableContract

替代旧 `TableSpec` 的核心契约名。

```python
TableContract(
    name="ticks",
    pk=["timestamp", "symbol"],
    columns={"timestamp": "integer", "symbol": "text", "price": "real"},
    partition=Partition.duration("timestamp", every="1day"),
    segment=Segment.rows(max_rows=500_000),
    order_key="timestamp",
    conflict="append",
)
```

职责：

- 持久化表结构和布局策略。
- 重开数据库后恢复一致行为。
- 拒绝与既有契约冲突的写入。

不包含：

- `time`
- `TimeColumn`
- timeseries 专用查询参数

### PartitionPolicy

公开为：

```python
Partition.range(column, every)
Partition.duration(column, every)
Partition.calendar(column, every="1day", timezone="UTC")
Partition.value(column)
Partition.hash(column, buckets)
```

第一阶段建议实现：

- `range`
- `duration`
- `calendar`

第二阶段再实现：

- `value`
- `hash`

### SegmentPolicy

公开为：

```python
Segment.rows(max_rows=500_000, target_rows=None)
Segment.bytes(max_bytes="256MB")
Segment.rows_or_bytes(max_rows=500_000, max_bytes="256MB")
```

第一阶段建议只实现：

```python
Segment.rows(max_rows=500_000)
```

### QueryPlan

`explain_select` 必须暴露逻辑分区和物理 segment 两层。

```python
plan = table.explain_select(
    where={"field": "timestamp", "op": "between_and", "value": [start, end]},
    order_by="timestamp",
    limit=100,
)
```

建议字段：

```text
candidate_partitions
scanned_partitions
skipped_partitions
candidate_segments
scanned_segments
skipped_segments
skip_reasons
requires_global_sort
requires_python_limit
```

`explain_select` 是核心能力，不应只存在于 timeseries `query()`。

## 写入模型

### 无 Segment

如果没有 `segment`：

```text
row -> PartitionPolicy -> logical_part -> one SQLite file
```

未配置 `partition` 时就是单个 SQLite 文件。

### 有 Segment

如果配置了 `segment`：

```text
row -> PartitionPolicy -> logical_part -> active segment -> SQLite file
```

没有 `partition` 时：

```text
row -> "__all__" -> active segment -> SQLite file
```

### Segment 状态

```text
active       正在写入
sealing      正在封存，阻止新写入进入
sealed       已封存，只读
compacting   正在参与合并
obsolete     已被替代，可清理
```

### Manifest 是权威目录

必须有 manifest 表记录：

```text
table_name
logical_part
segment_id
part_key
path
state
row_count
created_at
sealed_at
generation
stats_json
```

`stats_json` 记录列级统计：

```json
{
  "timestamp": {"min": 1767225600000, "max": 1767311999999},
  "seq": {"min": 1000000, "max": 1499999},
  "symbol": {"sample_values": ["AAPL", "MSFT"]}
}
```

第一阶段只要求 min/max。Bloom filter、small value set 可以后续加。

## 晚到数据如何处理

如果顺序插入，segment 范围通常很紧：

```text
segment 000000: 00:00-08:00
segment 000001: 08:00-16:00
segment 000002: 16:00-24:00
```

如果后期补写中间数据，不应强行打开旧 sealed segment 修改。推荐写入新的 active/delta segment：

```text
logical partition: 2026-01-01
  segment 000000: 00:00-08:00 sealed
  segment 000001: 08:00-16:00 sealed
  segment 000002: 16:00-24:00 sealed
  segment 000003: 09:00-10:00 sealed/delta
```

查询 `09:00-10:00` 时必须扫描：

```text
segment 000001
segment 000003
```

这会增加查询文件数，但保证写入简单、并发安全、sealed segment 稳定。

后续通过 compaction 恢复布局：

```text
base segments + delta segments -> new compacted segments
old segments -> obsolete
```

## 查询影响

允许多个 segment 隶属于同一个逻辑时间段或范围段，会带来明确影响。

### 1. 查询不能只靠 logical partition 裁剪

以前可以假设：

```text
2026-01-01 -> 一个文件
```

最终模型必须假设：

```text
2026-01-01 -> 多个文件
```

查询流程变成：

```text
1. 用 where 裁剪 logical partitions。
2. 展开 logical partitions 下的 candidate segments。
3. 用 segment stats 裁剪 segments。
4. 扫描剩余 SQLite 文件。
5. 必要时全局排序、去重、limit。
```

### 2. `order_by` 需要全局处理

只要多个 segment 的 `order_key` 范围有重叠，就不能假设文件天然有序。

```text
segment 000001: 08:00-16:00
segment 000003: 09:00-10:00
```

查询 `ORDER BY timestamp LIMIT 100` 时，不能先从 `000001` 取 100 条就结束，必须和 `000003` 合并后再截断。

### 3. `limit` 不能随意下推

只有在以下条件满足时，`limit` 才能安全下推：

- 查询有明确 `order_by`。
- segment 范围互不重叠。
- 扫描顺序与 `order_by` 一致。
- 不存在需要去重或 tombstone 合并的旧版本。

否则必须先全局合并，再应用 limit。

### 4. explain 必须让成本可见

用户需要知道为什么查询变慢：

```text
scanned_partitions = ["20260101"]
scanned_segments = ["20260101__000001", "20260101__000003"]
skipped_segments = ["20260101__000000", "20260101__000002"]
skip_reasons = {"20260101__000000": "stats.timestamp.max < query.min"}
requires_global_sort = true
```

## 主键和冲突策略

行数分段会放大跨 segment 主键问题。

最终设计应显式区分两种表。

### append 表

```python
events = db.table(
    "events",
    pk=["event_id"],
    segment=Segment.rows(max_rows=500_000),
    conflict="append",
)
```

语义：

- 写入最快。
- 适合 event log、immutable facts。
- 不主动跨 sealed segments 查重。
- 重复主键由用户避免，或只在 active segment 内检查。

### keyed 表

```python
items = db.table(
    "items",
    pk=["item_id"],
    segment=Segment.rows(max_rows=500_000),
    conflict="upsert",
)
```

需要全局主键索引：

```text
_iosql_pk_index
  table_name
  pk_hash
  pk_json
  logical_part
  segment_id
  rowid
  generation
  deleted
```

upsert 规则：

1. 查全局 pk index。
2. 如果旧行在 active segment，可原地 replace。
3. 如果旧行在 sealed segment，不直接修改旧文件。
4. 写入新版本到 active segment。
5. 标记旧版本 tombstone。
6. compaction 清理旧版本。

第一阶段如果实现成本过高，可以只支持 `conflict="append"` + row segment。`conflict="upsert"` 必须等全局 pk index 和 tombstone 模型准备好再开放。

## Compaction

Compaction 不是可选优化，而是 row segment 设计的必要组成部分。

触发条件：

- 同一 logical partition 下 delta segments 过多。
- segment stats 范围重叠严重。
- obsolete/tombstone 比例过高。
- 小 segment 数量过多。

API：

```python
table.compact()
table.compact(partition="20260101")
table.compact(max_segments=20)
```

Compaction 输出：

```text
new sealed segments
updated manifest generation
old segments marked obsolete
```

文件删除应延迟到没有活跃查询引用旧 generation 后再执行。

## 文件布局

推荐目录结构：

```text
data/app.iosql/
  _iosql.sqlite3                 # manifest
  tables/
    ticks/
      parts/
        20260101/
          000000.sqlite3
          000001.sqlite3
          000002.sqlite3
        20260102/
          000000.sqlite3
      all/
        000000.sqlite3           # 无 logical partition 时可用
```

manifest SQLite 只保存元数据，不保存用户表大数据。

每个 segment SQLite 文件内部表名保持用户表名或稳定内部名。推荐内部统一为用户表名，方便用户用 sqlite3 直接打开检查。

## 元数据表

### `_iosql_tables`

```text
table_name
contract_json
created_at
updated_at
generation
```

### `_iosql_partitions`

```text
table_name
logical_part
partition_min
partition_max
segment_count
row_count
stats_json
generation
```

### `_iosql_segments`

```text
table_name
logical_part
segment_id
part_key
path
state
row_count
stats_json
created_at
sealed_at
generation
```

### `_iosql_pk_index`

仅 keyed/upsert 表需要。

```text
table_name
pk_hash
pk_json
logical_part
segment_id
rowid
generation
deleted
```

## 应删除或降级的旧概念

既然可以按重构思路实施，建议直接清理：

### 删除核心层 `TimeColumn`

`TimeColumn` 不是自动分区表的核心概念。

如果 `timeseries()` helper 需要时区、单位、date query，可以在 helper 私有配置里处理，不进入 `TableContract` 核心字段。

### 删除核心层 `query(start/end/date)`

核心查询统一为：

```python
table.select(fields="*", where=..., order_by=..., limit=...)
table.explain_select(where=...)
```

`query(start/end/date)` 如果保留，只能作为 `timeseries()` 返回对象上的 helper。

### 删除可变 `table.partition = ...`

分区策略是表契约的一部分，不应运行时任意改。

应统一通过：

```python
db.table("name", partition=...)
```

或未来显式迁移：

```python
table.repartition(...)
```

### 删除每次写入传 `pk` / `partition` 的主路径

旧写法：

```python
table.insert_all(rows, pk=[...], partition=...)
```

容易让同一表不同批次出现不同契约。

最终主路径应是：

```python
table = db.table("name", pk=[...], partition=...)
table.insert_all(rows)
```

首次写入 bootstrap 可以后续作为 convenience 评估，但不是核心设计。

### 重命名 `TableSpec`

建议改为 `TableContract`。

如果短期实现成本考虑保留类名，也应把字段改成新模型，不再让 `time` 出现在核心结构中。

## 实施顺序

### 阶段 1：核心模型重构

- 新建 `TableContract`。
- 新建 `Partition` 工厂：`range`、`duration`、`calendar`。
- 新建 `Segment` 工厂：`rows`。
- 移除核心 `TimeColumn` 依赖。
- `Database.table(...)` 成为唯一建表入口。
- `Table.select(...)` 和 `Table.explain_select(...)` 成为核心查询入口。

### 阶段 2：manifest 与 segment

- manifest 中区分 logical partition 和 physical segment。
- `_iosql_segments` 增加通用 `stats_json`。
- 写入时维护 active/sealed segment。
- 查询时执行 partition pruning + segment pruning。

### 阶段 3：晚到数据和 compaction

- 晚到数据写入 active/delta segment。
- explain 展示重叠 segment 和全局排序需求。
- 实现 `compact(...)`。

### 阶段 4：高级能力

- 全局 pk index。
- tombstone。
- keyed/upsert segment 表。
- bytes segment。
- hash/value partition。
- Bloom filter 或 small value set stats。

### 阶段 5：timeseries helper

- 基于新核心模型重新实现 `db.timeseries(...)`。
- helper 可以提供 `query(start/end)`、`latest`、`asof`。
- helper 不应反向污染核心 `TableContract`。

## 最终取舍

如果目标是“更接近原生 SQLite，仅增加自动分区能力”，最终选择是：

1. `Database.table(...)` 是唯一主入口。
2. `TableContract` 不包含 `time`。
3. `Partition` 只表达按字段值的逻辑路由。
4. `Segment` 只表达按物理大小的滚动策略。
5. `select(where=...)` 是核心查询 API。
6. `explain_select(...)` 必须展示 logical partitions 和 physical segments。
7. 晚到数据写新 segment，不修改旧 sealed segment。
8. 多 segment 隶属同一时间段是允许的，但必须通过 stats、global sort 标记和 compaction 管理查询代价。
9. `timeseries()` 可以保留为新核心之上的 helper，不再是核心模型。
10. 旧的围绕 `TimeColumn/TableSpec/query(start/end)` 的代码，如果不再服务新模型，应删除或下沉。
