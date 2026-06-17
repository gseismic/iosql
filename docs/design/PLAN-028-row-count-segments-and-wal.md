# PLAN-028-row-count-segments-and-wal

## 背景

`PLAN-027-partitioned-sqlite-core-api` 已经把核心 API 从 `time + query(start/end)` 调整为更接近 SQLite 的：

```text
partition + select(where)
```

但如果单位时间内的数据密度不可预测，单纯按时间或数值范围分区仍然会出现两个问题：

- 某些时间段数据极密，单个分区文件过大。
- 某些时间段数据极稀，分区文件太多太小。

用户希望支持“按数据条数分区”，并类比 SQLite WAL 模式：写入先追加到活跃区域，达到阈值后滚动/封存，查询通过 manifest 找到需要扫描的文件。

这个设计稿补充 `PLAN-027`：核心应区分“按值路由的逻辑分区”和“按行数滚动的物理 segment”。

## 结论先行

按行数分区不应被建模成普通 `Partition.range(...)`，因为它不是由一行里的字段值稳定计算出来的分区 key，而是由写入顺序和文件容量决定。

推荐设计：

```python
from iosql import Database, Segment

db = Database("sqlite://data/app.iosql")

events = db.table(
    "events",
    pk=["event_id"],
    columns={"event_id": "integer", "seq": "integer", "kind": "text"},
    segment=Segment.rows(max_rows=500_000, target_rows=400_000),
)

events.insert_all(rows)

rows = events.select(
    "*",
    where={"field": "seq", "op": "between_and", "value": [10_000_000, 10_100_000]},
    order_by="seq",
    to_dict=True,
)
```

如果希望保留 `partition=` 的单一入口，可提供一个语法糖：

```python
events = db.table(
    "events",
    pk=["event_id"],
    partition=Partition.rows(max_rows=500_000),
)
```

但内部仍应归一化为：

```python
partition=None
segment=Segment.rows(max_rows=500_000)
```

设计原则：

- `Partition` 负责按字段值确定逻辑分区。
- `Segment` 负责控制物理文件大小和滚动策略。
- 行数驱动属于 `Segment`，不是传统 `Partition`。
- 查询裁剪依赖 segment manifest 的统计信息，而不是直接由 where 计算 partition key。

## 为什么按行数分区不等同于按字段分区

按字段分区是确定性的：

```python
Partition.range("seq", every=1_000_000)
```

只要看到一行数据的 `seq`，就能直接算出它应该写到哪个分区。

按行数分区是写入顺序相关的：

```text
第 1 到 500000 行 -> segment 000000
第 500001 到 1000000 行 -> segment 000001
```

如果中间有 delete、upsert、ignore、并发写入或批量重放，仅凭一行数据无法计算它属于哪个 segment。系统必须依赖 manifest 和主键索引维护位置。

所以更准确的模型是：

```text
逻辑表
  -> 可选逻辑分区 partition
  -> 每个逻辑分区下有多个 row-bounded segment
```

没有逻辑分区时，所有数据属于一个默认逻辑分区：

```text
logical_part = "__all__"
segment_id = 0, 1, 2, ...
```

## 两个正交维度

### 1. PartitionPolicy：按值路由

负责回答：

- 这一行属于哪个逻辑分区？
- where 条件能否计算候选逻辑分区？

示例：

```python
Partition.range("seq", every=1_000_000)
Partition.duration("timestamp", every="1day")
Partition.calendar("event_date", every="1day")
Partition.value("tenant_id")
Partition.hash("user_id", buckets=128)
```

### 2. SegmentPolicy：按物理大小滚动

负责回答：

- 当前写入应追加到哪个物理文件？
- 活跃 segment 什么时候封存？
- 一个 segment 的目标行数、最大行数、最大字节数是多少？

示例：

```python
Segment.rows(target_rows=400_000, max_rows=500_000)
Segment.bytes(target_bytes="256MB")
Segment.rows_or_bytes(max_rows=500_000, max_bytes="256MB")
```

这两个维度可以组合：

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    partition=Partition.duration("timestamp", every="1day"),
    segment=Segment.rows(max_rows=500_000),
)
```

也可以只用 segment：

```python
events = db.table(
    "events",
    pk=["event_id"],
    segment=Segment.rows(max_rows=500_000),
)
```

## WAL-like 写入模型

### 目标

类比 SQLite WAL 的核心思想：

1. 写入优先追加到活跃区域。
2. 活跃区域达到阈值后 checkpoint / seal。
3. 已 seal 的文件成为稳定只读段。
4. 查询同时读取活跃段和已 seal 段。
5. manifest 是权威目录，记录哪些段存在、状态和统计信息。

### 状态模型

建议 segment 状态：

```text
active       正在写入
sealing      正在封存，防止并发继续追加
sealed       已封存，只读
compacting   正在参与合并/重写
obsolete     已被新 segment 取代，可后台清理
```

### Manifest

每个 segment 记录：

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
min/max stats by column
bloom/filter stats by column
```

当前代码已经有 `_iosql_segments` 和 `SegmentInfo`，但字段偏时序：

```text
time_min
time_max
session_min
session_max
```

行数分区需要升级为通用统计：

```text
key_min
key_max
column_stats: {"seq": {"min": ..., "max": ...}, "tenant_id": {"values": ...}}
```

短期兼容可以保留 `time_min/time_max`，同时新增 `stats_json`。

### 写入流程

无逻辑分区时：

```text
1. 读取 manifest 中 logical_part="__all__" 的 active segment。
2. 如果没有 active segment，创建 segment 0。
3. 在 SQLite transaction 中写入 rows。
4. 更新 row_count 和 column stats。
5. 如果 row_count >= max_rows，标记 segment 为 sealed。
6. 下一批写入创建 segment 1。
```

有逻辑分区时：

```text
1. 用 PartitionPolicy 计算 rows 的 logical_part。
2. 每个 logical_part 单独维护 active segment。
3. segment_id 在 logical_part 内递增。
4. 文件名可以是 {logical_part}__{segment_id}.sqlite3。
```

## 查询裁剪

按行数滚动的 segment 不能仅由 where 直接算出候选文件。需要两层裁剪。

### 第一层：逻辑分区裁剪

如果表有 `PartitionPolicy`：

```python
Partition.range("seq", every=1_000_000)
```

where 命中分区字段时，可以先裁剪 logical partitions。

如果表只有 `Segment.rows(...)`，没有逻辑分区，则第一层只有一个默认逻辑分区 `__all__`。

### 第二层：segment stats 裁剪

每个 segment 记录统计信息：

```json
{
  "seq": {"min": 1000000, "max": 1499999},
  "event_id": {"min": 9000, "max": 12000},
  "tenant_id": {"bloom": "..."}
}
```

查询：

```python
where={"field": "seq", "op": "between_and", "value": [1_100_000, 1_200_000]}
```

系统跳过所有 `seq.max < 1_100_000` 或 `seq.min > 1_200_000` 的 segment。

对于等值/in 查询：

- 如果有 min/max，先做范围粗裁剪。
- 如果有 bloom filter 或 small value set，再进一步裁剪。

如果没有对应 stats，则保守扫描。

## API 设计

### 1. 仅按行数滚动

```python
events = db.table(
    "events",
    pk=["event_id"],
    segment=Segment.rows(max_rows=500_000),
)

events.insert_all(rows)
```

适合：

- 单位时间密度不可预测。
- 不想提前选择时间/范围分区。
- 主要需要控制 SQLite 文件大小。

### 2. 按字段分区 + 每个分区内按行数滚动

```python
ticks = db.table(
    "ticks",
    pk=["timestamp", "symbol"],
    partition=Partition.duration("timestamp", every="1day"),
    segment=Segment.rows(max_rows=500_000),
)
```

适合：

- 希望按日期/租户/范围进行第一层目录划分。
- 又担心单个逻辑分区过大。

### 3. 自动选择：density unknown

提供更高级策略：

```python
events = db.table(
    "events",
    pk=["event_id"],
    partition=Partition.auto(
        by="timestamp",
        prefer="rows",
        target_rows=400_000,
        max_rows=500_000,
    ),
)
```

但不建议第一阶段实现。`auto` 容易让用户以为系统会自动改变历史布局；这会增加重写、并发和查询复杂度。

第一阶段更明确：

```python
segment=Segment.rows(max_rows=500_000)
```

### 4. WAL-like buffer

当前代码已有内存 `write_buffer(max_rows=...)`，但它不是持久化 WAL。可以设计持久化 ingest log：

```python
with table.ingest_log(max_rows=100_000, checkpoint="manual") as log:
    log.append(rows)
    log.checkpoint()
```

或者：

```python
table.insert_all(rows, durable_log=True)
table.checkpoint()
```

内部：

```text
_iosql_ingest/
  000000.log.sqlite3
  000001.log.sqlite3

_iosql_segments/
  000000.sqlite3
  000001.sqlite3
```

写入先进入 log，checkpoint 时批量写入 row-bounded segment。崩溃恢复时重放未 checkpoint 的 log。

第一阶段可以不做持久化 log，只做 row-bounded active segment。因为 SQLite 自己的 WAL 已能保证单个 SQLite 文件事务安全；真正的 ingest log 是更高层的跨 segment 崩溃恢复能力。

## 与 SQLite WAL 的类比和差异

相似点：

- 都是 append-first。
- 都有 active 写入区域。
- 都有 checkpoint / seal。
- 都通过 metadata 判断哪些数据已稳定。

差异：

- SQLite WAL 是单个数据库文件的页级日志。
- iosql row segment 是表级、文件级的数据段。
- SQLite checkpoint 把页合并回主库；iosql checkpoint 把 ingest log 写成新的 sealed segment。
- SQLite WAL 对用户透明；iosql segment manifest 是查询计划的一部分。

所以命名上不建议把它直接叫 WAL partition。更准确：

```text
row-bounded segment layout
durable ingest log
checkpoint to segments
```

## 主键和 upsert 问题

按行数滚动会放大跨 segment 主键问题。

如果默认 `insert_all` 是 append，重复主键可以交给单 segment SQLite 约束，但无法阻止重复主键出现在旧 segment 和新 segment。

因此要明确两条路径：

### append-only 表

```python
events = db.table(
    "events",
    pk=["event_id"],
    segment=Segment.rows(max_rows=500_000),
    conflict="append",
)
```

语义：

- 写入快。
- 不保证跨 segment upsert。
- 适合 event log、immutable facts。
- 重复主键是用户错误，可选择在当前 active segment 内报错。

### keyed/upsert 表

```python
items = db.table(
    "items",
    pk=["item_id"],
    segment=Segment.rows(max_rows=500_000),
    conflict="upsert",
)
```

需要维护全局主键索引：

```text
_iosql_pk_index
  pk_hash
  pk_json
  logical_part
  segment_id
  rowid
  generation
```

upsert 时：

1. 先查 `_iosql_pk_index`。
2. 找到旧行所在 segment。
3. 对 active segment 可原地 replace。
4. 对 sealed segment 不建议原地修改；更像 LSM：写入新版本并标记旧版本 tombstone。
5. compaction 时清理旧版本。

第一阶段如果不想引入 LSM/tombstone，应限制：

- `Segment.rows(...)` 默认推荐 append-only。
- `write(mode="upsert")` 仍可用，但需要说明跨 segment 会更慢，可能扫描 manifest 或 pk index。

当前代码已经有跨 segment 主键查找，但实现是扫描当前逻辑分区内 segment，适合小规模，不适合作为最终大规模方案。

## Compaction / vacuum

行数 segment 长期运行后会出现：

- 删除造成空洞。
- upsert 造成旧版本。
- 小 segment 太多。
- stats 过期。

需要设计 compaction：

```python
result = table.compact(
    where=None,
    target_rows=400_000,
    max_rows=500_000,
)
```

或：

```python
table.vacuum_segments()
```

规则：

1. 读取多个 sealed segments。
2. 去掉 tombstone 和旧版本。
3. 重新写入新的 sealed segments。
4. manifest 原子切换 generation。
5. 旧 segments 标记 obsolete，后台删除。

这与 SQLite `VACUUM` 类似，但作用在 iosql 的 segment 层。

## 文件布局建议

无逻辑分区：

```text
table/
  meta.sqlite3
  data/
    __all__/
      000000.sqlite3
      000001.sqlite3
      000002.sqlite3
```

有逻辑分区：

```text
table/
  meta.sqlite3
  data/
    20260101/
      000000.sqlite3
      000001.sqlite3
    20260102/
      000000.sqlite3
```

当前代码使用 `logical_part__segment_id.sqlite3`，短期可以继续沿用：

```text
20260101__000000.sqlite3
20260101__000001.sqlite3
```

长期目录分层更清晰，但迁移成本更高。

## 查询计划输出

需要把 explain 从“时间分区”改成通用术语：

```python
plan = table.explain_select(where=...)
```

输出：

```python
QueryPlan(
    candidate_partitions=[...],
    scanned_partitions=[...],
    skipped_partitions=[...],
    candidate_segments=[...],
    scanned_segments=[...],
    skipped_segments=[...],
    segment_skip_reasons={
        "000001": "pruned_by_segment_stats",
        "000002": "pruned_by_partition_key",
    },
)
```

避免继续使用：

```text
pruned_by_segment_time_range
time_min/time_max
```

改为：

```text
pruned_by_segment_key_range
key_min/key_max
stats_min/stats_max
```

## 实施建议

### 阶段 1：暴露 row-bounded segment，不做持久化 ingest log

新增：

```python
Segment.rows(target_rows=400_000, max_rows=500_000)
```

扩展：

```python
Database.table(..., segment=Segment.rows(...))
```

内部复用当前 `PhysicalLayout` 和 `_iosql_segments`。

需要补充：

- 没有 `partition` 时使用默认 logical part `__all__`。
- `SegmentInfo` 从 `time_min/time_max` 泛化为 `stats_json`。
- `select(where)` 使用 segment stats 裁剪。

### 阶段 2：持久化 ingest log

新增：

```python
table.ingest_log()
table.checkpoint()
table.recover()
```

写入先进入 log，checkpoint 进入 segment。

### 阶段 3：全局主键索引和 tombstone

新增：

```text
_iosql_pk_index
_iosql_tombstones
```

支持高效跨 segment upsert/delete。

### 阶段 4：compaction

新增：

```python
table.compact()
table.vacuum_segments()
```

清理旧版本、合并小文件、刷新 stats。

## 对 PLAN-027 的补充

`PLAN-027` 提出的核心是正确的：

```text
partition + select(where)
```

但它还需要补充一个正交维度：

```text
segment_by_rows
```

最终核心 API 应是：

```python
db.table(
    "events",
    pk=["event_id"],
    partition=...,  # 可选，按字段值路由
    segment=...,    # 可选，按物理大小滚动
)
```

如果用户不确定单位时间数据密度，推荐优先使用：

```python
segment=Segment.rows(max_rows=500_000)
```

而不是强行选：

```python
partition=Partition.duration("timestamp", every="1day")
```

也可以组合两者，在逻辑分区内继续按行数滚动。

## 关键约束

1. 行数分区依赖写入顺序，不能保证由 row value 直接计算分区 key。
2. 行数 segment 必须有 manifest；manifest 是查询计划和恢复的权威来源。
3. append-only 和 upsert 表要区分，否则跨 segment 主键语义会混乱。
4. 持久化 ingest log 是 WAL-like 能力，不等于 SQLite 自身 WAL。
5. 没有 stats 时必须保守扫描，不能为了裁剪而漏查。
6. compaction 必须是生成式和可回滚的，不应原地破坏旧 segment。

## 最终选择

支持“按数据条数分区”是必要的，但建议以 `Segment.rows(...)` 表达，而不是让 `Partition` 同时承担两种语义。

推荐心智模型：

```text
Partition：数据按什么业务 key 分组。
Segment：物理文件如何滚动和封存。
Ingest log：可选的持久化写入缓冲和恢复层。
```

这样既能保留接近 SQLite 的使用方式，又能处理单位时间数据密度不确定的问题。
