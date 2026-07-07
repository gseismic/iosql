# iosql API 参考

## 目录

- [核心入口](#核心入口)
- [分区策略](#分区策略)
- [Segment 配置](#segment-配置)
- [表契约](#表契约)
- [结果类型](#结果类型)
- [查询类型](#查询类型)
- [异常类型](#异常类型)
- [低层 mysqlite](#低层-mysqlite)

---

## 核心入口

### `Database(uri, verbose=0, pragma_dict=None, max_open_partitions=5, max_connections_per_file=1, max_process_connections=32)`

数据库入口。支持 `sqlite://` 和 `postgresql://` URI。

**SQLite URI 格式**：`sqlite://path/database_name.iosql`

**PostgreSQL URI 格式**：`postgresql://user:pass@host:port/dbname`

PostgreSQL 功能需要安装可选依赖：

```bash
python -m pip install "iosql[postgresql]"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `uri` | `str` | 数据库 URI，如 `"sqlite://db/test.iosql"` 或 `"postgresql://user:pass@localhost:5432/mydb"` |
| `verbose` | `int` | 日志详细度，`0`=静默 |
| `pragma_dict` | `dict`/`None` | 仅 SQLite；覆盖默认 balanced PRAGMA，例如 `{"synchronous": "full"}` |
| `max_open_partitions` | `int` | 仅 SQLite；每张表最多缓存的打开分区数据库数，默认 `5` |
| `max_connections_per_file` | `int` | 仅 SQLite；每个 SQLite 文件的连接池上限，默认 `1` |
| `max_process_connections` | `int` | 仅 SQLite；进程内所有 mysqlite 池共享的真实连接预算，默认 `32` |

三个资源参数必须是正整数。分区数据库按带租约的线程安全 LRU 淘汰；两个元数据库
不计入 `max_open_partitions`，但计入 `max_process_connections`。多个活跃实例
配置不同时，当前进程采用其中最小的进程预算。WAL 模式通常还会打开 `-wal` 和
`-shm`，因此文件描述符数量高于连接数。

**上下文管理器**：

```python
with Database("sqlite://db/test.iosql") as db:
    table = db.table("data", ...)
# 自动 close()
```

#### `Database.table(table_name, spec=None, pk=None, primary_key=None, columns=None, partition=None, segment=None, order_key=None, conflict='append', schema_policy='evolve')`

核心表入口。创建或打开一张分区表。

| 参数 | 类型 | 说明 |
|------|------|------|
| `table_name` | `str` | 表名 |
| `spec` | `TableSpec`/`TableContract` | 完整表契约对象 |
| `pk` | `str`/`list[str]` | 主键（与 `spec` 互斥） |
| `primary_key` | `str`/`list[str]` | 主键别名（与 `spec` 互斥，与 `pk` 二选一） |
| `columns` | `dict[str,str]` | 列名→类型映射 |
| `partition` | `Partition` | 分区实例 |
| `segment` | `Segment` | 物理分段配置 |
| `order_key` | `str` | 排序键（用于 segment 统计） |
| `conflict` | `str` | 冲突策略：`"append"`/`"upsert"`/`"replace"`/`"ignore"` |
| `schema_policy` | `str` | Schema 策略：`"evolve"`（自动加列） |

#### `Database.timeseries(table_name, time, primary_key, columns=None, partition_every='1day', timezone='UTC', schema_policy='evolve', symbol_column='symbol', session_column=None, target_rows_per_file=400_000, min_rows_per_file=300_000, max_rows_per_file=500_000, time_unit='ms', partition_kind='duration')`

时序表快捷创建入口。内部构造 `TableContract.timeseries()` 后调用 `table(spec=...)`。

#### `Database.table_names() -> list[str]`

列出数据库中已存在的表名。

PostgreSQL 后端只返回用户逻辑表，不返回 native partition 子表。

#### `Database.rename(new_uri_or_name, exist_ok=False, overwrite=False) -> Database`

重命名当前 SQLite 目录库。`new_uri_or_name` 可以是完整 `sqlite://...`
URI，也可以是当前库父目录下的新目录名；重命名成功后当前 `Database`
实例的 `uri` 会刷新到新路径，已缓存的表对象也会重新绑定到新目录。

PostgreSQL database 重命名不通过 iosql 执行。目标已存在时默认抛出
`BadArgument`；需要覆盖时传 `overwrite=True`。

#### `Database.rename_table(old_name, new_name, exist_ok=False, overwrite=False) -> Table`

重命名逻辑表并维护当前 `Database` 的表缓存。SQLite 后端会移动表目录并更新
表内 manifest 的 `table_name` 字段；PostgreSQL 后端会重命名真实关系表，
native partition 模式下会同步重命名旧表名前缀的子分区表。

目标表已存在时默认抛出 `BadArgument`；需要覆盖时传 `overwrite=True`。

#### `Database.close()`

关闭所有表和连接。

#### `Database.get_process_connection_budget_status()`

返回 SQLite 进程级连接预算状态，包括 `max_process_connections`、
`open_process_connections` 和 `registered_pools`。

#### `Database.migrate_to(target_uri, tables=None, mode='append', batch_size=10_000, verify=None, dry_run=False) -> MigrationResult`

把当前 iosql 库迁移到目标库。典型用途是 SQLite 到 PostgreSQL：

```python
source = Database("sqlite://db/market.iosql")
result = source.migrate_to(
    "postgresql://user:pass@localhost:5432/market",
    tables=["daily_bars", "kline_1m"],  # None 表示全库
    batch_size=50_000,
    verify="row-count",
)
```

`tables` 可传：

- `None`：迁移全库。
- `["ticks", "daily"]`：迁移指定表，目标表同名。
- `{"ticks": "pg_ticks"}`：源表到目标表映射。

`dry_run=True` 只统计源表行数和估算批次数，不连接目标库。`verify="row-count"`
会在迁移后比较源/目标行数，结果写入 `source_rows`、`target_rows` 和 `verified`。

`to_postgresql(target_uri, ...)` 是只接受 `postgresql://` 目标的语义别名。
PostgreSQL 也可以作为迁移源写回 SQLite；重新打开 PostgreSQL 逻辑表时需要先
用 `db.table("name", spec=...)` 绑定同一份 `TableContract`。

---

### `Table` 对象方法

由 `Database.table()` 返回的表格对象提供以下方法：

#### 写入

| 方法 | 签名 |
|------|------|
| `write` | `(rows, mode=None, commit=True, **kwargs) -> WriteResult` |
| `write_buffer` | `(mode=None, max_rows=10_000, **write_kwargs) -> BufferedWriter` |
| `insert_all` | `(data, pk=None, extra_data=None, partition=None, replace=False, ignore=False, commit=True, **kwargs) -> WriteResult` |
| `upsert_all` | `(kv_list, pk, partition=None, commit=True, **kwargs)` |
| `delete` | `(where) -> int` |
| `truncate` | `(confirm=False) -> WriteResult` |

**写入模式**：

| 模式 | 说明 |
|------|------|
| `None` | 使用表契约 `conflict`，新表默认 `"append"` |
| `"append"` | 严格追加，重复主键通常由 SQLite 约束处理 |
| `"replace"` | 显式覆盖 |
| `"ignore"` | 重复主键跳过 |
| `"upsert"` | 按主键覆盖旧行；当前不支持 `Segment.rows` 表 |

#### 查询

| 方法 | 签名 |
|------|------|
| `query` | `(symbols=None, start=None, end=None, date=None, session=None, columns='*', where=None, order_by=None, limit=None) -> TableQuery` |
| `select` | `(fields, where, limit=None, order_by=None, to_dict=False, **kwargs) -> list` |
| `iter_select` | `(fields, where, limit=None, order_by=None, to_dict=False, batch_size=10_000) -> Iterator` |
| `iter_select_batches` | `(fields, where, limit=None, order_by=None, to_dict=False, batch_size=10_000) -> Iterator[list]` |
| `select_all` | `(fields, where, limit=None, to_dict=False, **kwargs) -> list` |
| `explain_select` | `(fields='*', where=None, order_by=None, limit=None) -> QueryPlan` |
| `latest` | `(symbols=None, as_of=None, columns='*', where=None, session=None, by=None, group_by=None) -> LatestQuery` |
| `asof` | `(symbols=None, as_of=None, columns='*', where=None, session=None, by=None, group_by=None) -> LatestQuery` |

#### 诊断

| 方法 | 签名 | 返回值 |
|------|------|--------|
| `spec` | `() -> TableContract\|None` | 表契约 |
| `table_parts` | `() -> list` | 逻辑分区列表 |
| `segments` | `(include_obsolete=False) -> list[dict]` | 物理 segment 清单 |
| `validate` | `(deep=False, include_obsolete=False) -> SegmentValidationResult` | 校验 manifest 与物理文件 |
| `layout_report` | `() -> LayoutReport` | 分区诊断报告 |
| `compact` | `(partition=None, target_rows=None) -> CompactResult` | 整理 row segment |
| `cleanup_obsolete` | `(partition=None, dry_run=True, validate=True) -> CleanupResult` | 清理 obsolete segment 文件 |
| `count_rows` | `(where=None) -> int` | 统计当前可见行数；PostgreSQL 支持 where |
| `rename` | `(new_name, exist_ok=False, overwrite=False) -> Table` | 重命名当前逻辑表 |
| `migrate_to` | `(target_uri, target_table=None, mode='append', batch_size=10_000, verify=None, dry_run=False) -> TableMigrationResult` | 迁移单表 |
| `repartition_to` | `(target_uri, table_name=None, spec=None, mode='append', batch_size=10_000) -> RepartitionResult` | 重分区结果 |
| `create_index` | `(column, order='asc', exist_ok=True)` | 创建索引 |

---

### `TableQuery` 对象

由 `table.query(...)` 返回。延迟执行，调用方法时触发实际查询。

| 方法 | 返回值 |
|------|--------|
| `.to_list()` | `list[dict]` - 查询结果 |
| `.explain()` | `QueryPlan` - 查询计划 |
| `.iter_rows()` | `Iterator[dict]` |
| `.iter_batches(batch_size=10_000)` | `Iterator[list[dict]]` |
| `.spec()` | `QuerySpec` - 查询规格 |

### 流式查询

```python
for row in table.iter_select("*", where=None, batch_size=50_000, to_dict=True):
    ...

for batch in table.iter_select_batches("*", where=None, batch_size=50_000, to_dict=True):
    ...

for batch in table.query(start=start, end=end).iter_batches(batch_size=50_000):
    ...
```

- `iter_select_batches()` 每批最多返回 `batch_size` 行。
- 无全局 `order_by` 时按分区/segment 分页读取，适合大结果集。
- 有 `order_by` 时为保证全局排序正确性，会复用 `select()` 的合并排序结果，再按批输出。
- `limit` 会跨 batch 生效。

### `LatestQuery` 对象

由 `table.latest(...)` 或 `table.asof(...)` 返回。

| 方法 | 返回值 |
|------|--------|
| `.to_list()` | `list[dict]` |
| `.one()` | `dict\|None` |
| `.explain()` | `QueryPlan` |
| `.iter_rows()` | `Iterator[dict]` |
| `.spec()` | `QuerySpec` |

### `BufferedWriter` 对象

由 `table.write_buffer(...)` 返回。上下文管理器。

| 方法 | 说明 |
|------|------|
| `.append(rows)` | 追加行，达到 `max_rows` 时自动 flush |
| `.flush()` | 强制 flush 缓冲区 |
| `.close()` | flush 并关闭 |
| `.results()` | 返回已 flush 的 `WriteResult` 列表 |

---

## 分区策略

### `Partition`（抽象基类）

```python
from iosql import Partition

# 工厂方法
Partition.duration("ts", every="1day")
Partition.calendar("date", every="1day", timezone="Asia/Shanghai")
Partition.range("seq", every=10000)
```

PostgreSQL native partition 下，`duration(...)` / `range(...)` 使用数值边界；
`calendar(...)` 支持 `text` / `date` / `timestamp` 目标列。text 日期列应统一
使用 ISO 日期（如 `2026-01-05`）或 compact 日期（如 `20260105`）。

### `DurationPartition`

| 参数 | 类型 | 说明 |
|------|------|------|
| `interval_ms` | `int`/`str` | 分区间隔（毫秒），也支持别名如 `"1day"` |
| `ts_col` | `str` | 时间戳列名 |

常用别名：`1min` `5min` `15min` `30min` `1hour` `4hour` `1day` `1week` `1month` `3month` `6month` `1year`

### `CalendarPartition`

| 参数 | 类型 | 说明 |
|------|------|------|
| `fmt` | `str` | 日期格式（`"YYYY"`/`"YYYYMM"`/`"YYYYMMDD"`/`"YYYYMMDDHH"`/`"YYYYMMDDHHmm"`） |
| `tz` | `str` | 时区（如 `"Asia/Shanghai"`） |
| `ts_col` | `str` | 日期列名 |

### `RangePartition`

| 参数 | 类型 | 说明 |
|------|------|------|
| `width` | `int` | 分区宽度 |
| `column` | `str` | 数值列名 |

---

## Segment 配置

### `Segment`

控制物理 SQLite 文件的按行数滚动。

```python
from iosql import Segment

# 三级阈值配置
Segment.rows(
    max_rows=500_000,
    target_rows=400_000,
    min_rows=300_000,
    stats_columns=["ts", "sequence"],
)
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `kind` | `"rows"` | 分段类型（当前仅支持 `"rows"`） |
| `max_rows` | `500_000` | 单文件最大行数 |
| `target_rows` | `max_rows` | 目标行数 |
| `min_rows` | `min(target_rows, max_rows)` | 最小行数 |

签名：

```python
Segment.rows(
    max_rows=500_000,
    target_rows=None,
    min_rows=None,
    stats_columns=None,
)
```

约束：`max_rows >= target_rows >= min_rows > 0`

`stats_columns=None` 默认统计全部列。显式列表可减少高频宽表的 min/max 维护成本；partition column、time、`order_key`、`session_column` 会自动补入必要统计列。

### Row Segment Compaction

```python
result = table.compact(partition=None, target_rows=None)
```

- 只支持启用 `Segment.rows(...)` 的 SQLite 表。
- `partition=None` 表示整理所有 logical partition。
- `target_rows=None` 时使用表契约里的 `target_rows_per_file`。
- compact 会把当前可见 segment 重写为新 generation 的 sealed segment。
- 旧 segment 标记为 `obsolete`；默认查询、`segments()`、`layout_report()` 不再使用它们。
- `segments(include_obsolete=True)` 可查看旧 segment 状态。
- compact 本身不删除旧 SQLite 文件；需要释放磁盘时使用 `cleanup_obsolete()`。
- compact 不做主键去重，不开放 `upsert/replace`；append-only 表中跨 segment 重复主键会被保留。

返回：

```python
CompactResult(
    table="ticks",
    partitions=[0],
    rows_read=1000,
    rows_written=1000,
    old_segments=4,
    new_segments=3,
    obsolete_segments=4,
)
```

### Segment Validate / Cleanup

```python
validation = table.validate(deep=True, include_obsolete=False)
preview = table.cleanup_obsolete()
cleanup = table.cleanup_obsolete(dry_run=False)
```

- `validate(deep=False)` 只做 manifest 级检查。
- `validate(deep=True)` 会检查当前可见 segment 的文件存在性、SQLite `integrity_check`、`data` 表、`row_count`、time/session 范围和 manifest stats。
- `include_obsolete=True` 会把 obsolete segment 纳入校验；obsolete 的深度问题以 warning 记录，不影响默认查询。
- `cleanup_obsolete()` 默认 dry-run，不删除文件。
- `cleanup_obsolete(dry_run=False)` 删除前默认执行 `validate(deep=True)`；校验失败会抛出 `UnsafeOperation`。
- 清理只删除 obsolete segment 主文件及同名 `-wal`/`-shm` 文件，并删除对应 manifest 行。

返回：

```python
SegmentValidationResult(
    table="ticks",
    deep=True,
    include_obsolete=False,
    checked_segments=3,
    errors=[],
    warnings=[],
)

CleanupResult(
    table="ticks",
    dry_run=False,
    partitions=[0],
    candidate_segments=4,
    files=[".../ticks/data/0__000001.sqlite3"],
    files_deleted=4,
    bytes_reclaimable=104857600,
    bytes_deleted=104857600,
    manifest_rows_deleted=4,
    skipped=[],
)
```

---

## 表契约

### `TableContract`（别名 `TableSpec`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `primary_key` | `list[str]` | 主键列 |
| `time` | `TimeColumn` | 业务时间列（可选） |
| `partition` | `PartitionSpec` | 分区规格 |
| `segment` | `Segment` | 物理分段 |
| `columns` | `dict[str,str]` | 列定义 |
| `schema_policy` | `str` | `"evolve"` |
| `order_key` | `str` | 排序键 |
| `conflict` | `str` | 冲突策略 |
| `symbol_column` | `str` | Symbol 列名（默认 `"symbol"`） |
| `session_column` | `str`/`None` | 交易日列名 |
| `physical_layout` | `PhysicalLayout` | 物理布局 |

#### `TableContract.timeseries(time, primary_key, ...)` 类方法

快捷构造时序表契约。

#### `TableContract.with_partition(every=None, kind=None, timezone=None, fmt=None)`

返回新契约，仅更改分区参数。

### `TimeColumn`

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 列名 |
| `unit` | `str` | 时间单位：`"ms"`/`"s"`/`"date"` |
| `timezone` | `str` | 时区 |

### `PartitionSpec`

| 字段 | 类型 | 说明 |
|------|------|------|
| `column` | `str` | 分区列名 |
| `every` | `Any` | 分区粒度 |
| `kind` | `str` | `"duration"`/`"calendar"`/`"range"` |
| `time_unit` | `str`/`None` | 时间单位 |
| `timezone` | `str`/`None` | 时区 |
| `fmt` | `str`/`None` | 日期格式 |

类方法：`by_time(column, every, timezone, kind)` / `range(column, every)`

---

## 结果类型

### `WriteResult`

| 字段 | 类型 | 说明 |
|------|------|------|
| `rows_seen` | `int` | 传入的总行数 |
| `rows_written` | `int`/`None` | 实际写入的行数（ignore 模式为 None） |
| `partitions_touched` | `list` | 涉及的分区 |
| `partitions_created` | `list` | 新创建的分区 |
| `schema_changed` | `bool` | Schema 是否变更 |
| `layout_warnings` | `list[str]` | 布局警告 |

### `LayoutReport`

| 字段 | 类型 | 说明 |
|------|------|------|
| `table` | `str` | 表名 |
| `target_rows_per_file` | `int` | 目标行数 |
| `min_rows_per_file` | `int` | 最小行数 |
| `max_rows_per_file` | `int` | 最大行数 |
| `partitions` | `list[PartitionStats]` | 各分区统计 |
| `recommendation` | `str` | 建议 |

| 属性 | 类型 | 说明 |
|------|------|------|
| `total_rows` | `int` | 总行数 |
| `total_partitions` | `int` | 分区总数 |
| `oversized_partitions` | `list` | 超大分区列表 |
| `undersized_partitions` | `list` | 过小分区列表 |

### `RepartitionResult`

| 字段 | 类型 | 说明 |
|------|------|------|
| `source_uri` | `str` | 源库 URI |
| `target_uri` | `str` | 目标库 URI |
| `table` | `str` | 表名 |
| `rows_copied` | `int` | 复制的行数 |
| `partitions_created` | `list` | 在目标库创建的分区 |

### `MigrationResult`

| 字段/属性 | 类型 | 说明 |
|------|------|------|
| `source_uri` | `str` | 源库 URI |
| `target_uri` | `str` | 目标库 URI |
| `tables` | `list[TableMigrationResult]` | 各表迁移结果 |
| `rows_copied` | `int` | 总迁移行数 |
| `batches` | `int` | 总写入批次数 |
| `source_rows` | `int\|None` | 源表总行数；dry-run 或 row-count 校验时可用 |
| `target_rows` | `int\|None` | 目标表总行数；row-count 校验时可用 |
| `verify` | `str\|None` | 校验模式 |
| `verified` | `bool\|None` | 校验是否通过 |
| `dry_run` | `bool` | 是否为预览结果 |

### `TableMigrationResult`

| 字段 | 类型 | 说明 |
|------|------|------|
| `table` | `str` | 源表名 |
| `target_table` | `str` | 目标表名 |
| `source_uri` | `str` | 源 URI |
| `target_uri` | `str` | 目标 URI |
| `rows_copied` | `int` | 迁移行数 |
| `batches` | `int` | 写入批次数 |
| `source_rows` | `int\|None` | 源表行数 |
| `target_rows` | `int\|None` | 目标表行数 |
| `verify` | `str\|None` | 校验模式 |
| `verified` | `bool\|None` | 校验是否通过 |
| `dry_run` | `bool` | 是否为预览结果 |

---

## 查询类型

### `QueryPlan`

| 字段 | 说明 |
|------|------|
| `candidate_partitions` | 候选逻辑分区 |
| `scanned_partitions` | 实际扫描的分区 |
| `skipped_partitions` | 跳过的分区 |
| `skip_reasons` | 跳过原因（dict） |
| `candidate_segments` | 候选物理 segment |
| `scanned_segments` | 实际扫描的 segment |
| `skipped_segments` | 跳过的 segment |
| `segment_skip_reasons` | segment 跳过原因（dict） |
| `requires_global_sort` | 是否需要 Python 层全局排序 |
| `requires_python_limit` | limit 是否在 Python 层执行 |

### `QuerySpec`

| 字段 | 说明 |
|------|------|
| `table` | 表名 |
| `columns` | 要查询的列 |
| `symbols` | Symbol 过滤 |
| `start` / `end` | 时间范围 |
| `date` | 日期过滤 |
| `session` | 交易日过滤 |
| `where` | 额外 where 条件 |
| `order_by` | 排序 |
| `limit` | 限制行数 |

---

## 异常类型

| 异常 | 说明 |
|------|------|
| `IosqlError` | 基类异常 |
| `BadArgument` | 参数错误 |
| `TableSpecConflict` | 表契约冲突（主键/分区/segment 不匹配） |
| `SchemaConflict` | Schema 冲突（同名列类型不一致） |
| `MissingPrimaryKey` | 缺少主键 |
| `MissingTimeColumn` | 缺少时间列 |
| `PartitionSpecError` | 分区规格错误 |
| `UnsafeOperation` | 不安全操作（如 truncate 未确认） |
| `QueryFieldError` | 查询字段不存在 |
| `ChunkVerificationError` | 数据块校验错误 |
| `EngineNotExist` | 不支持的存储引擎 |
| `InvalidUriSchema` | URI schema 无效 |

---

## 低层 mysqlite

### `iosql.utils.mysqlite.Database`

独立 SQLite 文件操作。基于文件路径（不需要 `sqlite://` 前缀）。

```python
from iosql.utils.mysqlite import Database

db = Database(
    "path/to/data.sqlite3",
    auto_commit=True,
    max_connections=4,
    max_process_connections=32,
)
table = db["table_name"]

# CRUD
table.insert(dict, pk=..., ensure_table=True, replace=False)
table.insert_all(list_of_dicts, pk=..., batch_size=5000)
table.select(fields, where=None, to_dict=True)
table.select_all(fields, where=None)
table.select_one(fields, where=None)
table.query_all(sql)
table.update(new_values, where)
table.delete(where)

# 连接池
db.get_connection_pool_status()
db.get_process_connection_budget_status()
db.is_connection_pool_healthy()
db.force_close_all_connections()

# 事务
with db.transaction():
    table.insert(...)

db.close()
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `auto_commit` | `True` | 自动提交 |
| `max_connections` | `10` | 最大连接数 |
| `timeout` | `5.0` | 连接超时（秒） |
| `wal` | `True` | 启用 WAL 模式 |

---

## 常量

| 常量 | 值 | 说明 |
|------|------|------|
| `DEFAULT_LOGICAL_PART` | `"__all__"` | 无逻辑分区时的默认分区名 |
| `__version__` | `str` | iosql 版本号 |

## Where 条件支持的运算符

| 运算符 | 别名 | 值类型 | 说明 |
|--------|------|--------|------|
| `==` | `=`, `eq` | 标量 | 等于 |
| `!=` | `neq` | 标量 | 不等于 |
| `>` | `gt` | 标量 | 大于 |
| `<` | `lt` | 标量 | 小于 |
| `>=` | `ge` | 标量 | 大于等于 |
| `<=` | `le` | 标量 | 小于等于 |
| `in` | — | `list` | 包含于 |
| `between_and` | — | `[min, max]` | 区间 |
