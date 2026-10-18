# iosql API 参考

## 目录

- [核心入口](#核心入口)
- [分区策略](#分区策略)
- [Segment 配置](#segment-配置)
- [表契约](#表契约)
- [结果类型](#结果类型)
- [查询类型](#查询类型)
- [异常类型](#异常类型)
- [可视化管理台](#可视化管理台)
- [低层 mysqlite](#低层-mysqlite)

---

## 核心入口

### `Database(uri, verbose=0, pragma_dict=None, max_open_partitions=5, max_connections_per_file=1, max_process_connections=32, max_ordered_read_connections=128)`

数据库入口。支持 `sqlite://`、`parquet://` 和 `postgresql://` URI。
Parquet 后端随 iosql 默认依赖提供 DuckDB，业务数据为 `data/*.parquet`，元数据为 SQLite。
Parquet 支持分区、segment、读写、流式排序、维护和迁移；追加、修改和删除会重写受影响文件。
Parquet 不支持 PRAGMA、持久化索引、`commit=False`、数据库目录 rename 和
`aggregate_by_bucket`。文件预算参数也可传给 Parquet，用于元数据连接及有序流预算；
接口细节见 [Parquet 使用指南](parquet.md)。

**SQLite URI 格式**：`sqlite://path/database_name.iosql`

**Parquet URI 格式**：`parquet://path/database_name.iosql`

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
| `max_connections_per_file` | `int` | SQLite 文件及 Parquet 元数据的连接池上限，默认 `1` |
| `max_process_connections` | `int` | 目录后端共享的元数据/SQLite 连接与有序流预算，默认 `32` |
| `max_ordered_read_connections` | `int` | SQLite/Parquet 单次带全局 `order_by` 的流式归并同时读取的分区上限，默认 `128` |

四个资源参数必须是正整数。分区数据库按带租约的线程安全 LRU 淘汰；两个元数据库
不计入 `max_open_partitions`，但计入 `max_process_connections`。多个活跃实例
配置不同时，当前进程采用其中最小的进程预算。WAL 模式通常还会打开 `-wal` 和
`-shm`，因此文件描述符数量高于连接数。

**上下文管理器**：

```python
with Database("sqlite://db/test.iosql") as db:
    table = db.table("data", ...)
# 自动 close()
```

#### `Database.table(table_name, spec=None, pk=None, primary_key=None, columns=None, partition=None, segment=None, order_key=None, conflict='append', schema_policy='evolve', force_partition=None, time=None, symbol_column=None, session_column=None, description=None, column_descriptions=None, column_ranges=None)`

核心表入口。创建或打开一张分区表。

| 参数 | 类型 | 说明 |
|------|------|------|
| `table_name` | `str` | 表名 |
| `spec` | `TableSpec`/`TableContract` | 完整表契约对象 |
| `pk` | `str`/`list[str]` | 主键（与 `spec` 互斥） |
| `primary_key` | `str`/`list[str]` | 主键兼容别名（与 `pk` 同传冲突时报错） |
| `columns` | `dict[str,str]` | 列名→类型映射 |
| `partition` | `Partition`/`str` | 分区实例或 `duration(...)` / `calendar(...)` / `range(...)` 字符串 |
| `segment` | `Segment` | 物理分段配置 |
| `order_key` | `str` | 排序键（用于 segment 统计） |
| `conflict` | `str` | 冲突策略：`"append"`/`"upsert"`/`"replace"`/`"ignore"` |
| `schema_policy` | `str` | Schema 策略：`"evolve"`（自动加列） |
| `time` | `str`/`TimeColumn` | 时间契约列；duration/calendar 分区未声明时自动反推 |
| `symbol_column` | `str` | 标的列；默认 `None`——未声明时 `query(symbols=)` / `latest(symbols=)` 抛 `BadArgument`（timeseries() 默认 `"symbol"`） |
| `session_column` | `str` | 交易日/session 列（期货夜盘等场景） |
| `description` | `str`/`None` | 表的业务说明 |
| `column_descriptions` | `dict`/`None` | 列名到业务说明的映射 |
| `column_ranges` | `dict`/`None` | 数值列到 `{"low": 下界, "high": 上界}` 的映射，只存声明，不校验写入 |

已有表上再次传 `spec=`（或快捷参数）时，声明的绑定字段
（`order_key`/`symbol_column`/`session_column`/`conflict`/`schema_policy`）会通过
`TableSpecConflict` 校验并写回持久化契约：存量仍为默认值或未声明时可被覆盖，
否则报 `TableSpecConflict`（含修复提示）。快捷参数路径省略 `force_partition` 时按 False 处理；`spec=` 路径省略时保留契约原值，显式传入与契约冲突的值会报错。`force_partition=True` 会跳过分区冲突检查并把新分区契约落盘。

#### `Database.timeseries(table_name, time, pk=None, primary_key=None, columns=None, *, partition_every='1day', timezone='UTC', symbol_column='symbol', session_column=None, partition=None, segment=None, order_key=None, conflict='append', schema_policy='evolve', force_partition=False, partition_kind=None, time_unit=None, target_rows_per_file=None, min_rows_per_file=None, max_rows_per_file=None, description=None, column_descriptions=None, column_ranges=None)`

时序语法糖：`table()` 的薄封装，duration 场景等价于 `db.table(..., partition=Partition.duration(time, every=partition_every), order_key=time, time=time)`。calendar 场景传 `partition=Partition.calendar(...)`。

废弃参数（触发 `DeprecationWarning`，行为等价迁移）：`partition_kind='calendar'` / `time_unit='date'` → 改传 `partition=Partition.calendar(...)`；`target/min/max_rows_per_file` → 改传 `segment=Segment.rows(...)`。

#### `Database.table_names() -> list[str]`

列出数据库中已存在的表名。

PostgreSQL 后端只返回用户逻辑表，不返回 native partition 子表。

#### `Database.rename(new_uri_or_name, exist_ok=False, overwrite=False) -> Database`

重命名当前 SQLite 目录库。`new_uri_or_name` 可以是完整 `sqlite://...`
URI，也可以是当前库父目录下的新目录名；重命名成功后当前 `Database`
实例的 `uri` 会刷新到新路径，已缓存的表对象也会重新绑定到新目录。

源目录与覆盖目标都必须通过 iosql 身份校验（存在表级 `<table>/meta.sqlite3`）；
根目录、家目录、符号链接目标一律拒绝。覆盖时先把目标改名为临时目录再替换，
失败自动回滚（跨文件系统 `EXDEV` 会包装为 `BadArgument` 并保留原数据）。

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

#### `Database.migrate_to(target_uri, tables=None, mode='append', batch_size=10_000, verify=None, dry_run=False, *, specs=None, temp_dir=None, lock_timeout=30.0) -> MigrationResult`

把当前 iosql 库迁移到目标库，支持 SQLite、Parquet、PostgreSQL 之间的迁移。
Parquet 的安装、写入成本和迁移示例见 [Parquet 使用指南](parquet.md)。

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

`dry_run=True` 只统计源表行数和估算批次数，不连接目标库；`mode`/`tables`/目标 URI
会在 `dry_run` 阶段一并校验，且不会为不存在的表创建目录或 `meta.sqlite3`。
本地源和目标不能相同或互相包含（`BadArgument`）；`temp_dir` 不能位于目标库内。
`tables` 支持 `None`（全库）、表名列表/字符串、或 `{源表: 目标表}` 映射；空列表、
未知源表或非法类型抛 `BadArgument`。`verify="row-count"` 要求各选中目标表不存在或为空，
并在发布前比较真实源/目标行数，结果写入 `source_rows`、`target_rows` 和 `verified`。
dry-run 不连接目标，因此不会验证目标连接、权限、已有表契约或空表条件。

两个接口的 `tables` 语义相同，Table 范围仅当前表。共同的 `specs` 按源名指定目标布局：`specs={"ticks":{"partition":"duration(ts,every=1hour)"}}`；值也可为完整 TableContract。允许 partition/segment/physical_layout/force_partition，保留源列、主键和时间含义。已有目标须兼容最终契约。

迁移持有源/目标独占锁，全部转换后统一发布。本地源默认在源库内暂存，PostgreSQL 源使用系统临时目录；本地目标有独立物理代际，PostgreSQL 目标使用暂存 schema 和事务提交。普通操作忙时立即抛 `DatabaseBusy`，迁移等待由 `lock_timeout` 控制。具体发布和恢复契约见 [design-contract.md](design-contract.md#统一迁移与安全发布plan-083)。Table 旧 `target_table` 和位置目标名仍可用，两类参数不能同时传。

迁移前关闭已消费的读取流、flush 内存缓冲、退出 `buffered_writer` 上下文并结束未提交事务。
`mode="replace"` 是主键冲突策略，不清空或重建目标；结果字段与异常排查见下文及
[troubleshooting.md](troubleshooting.md#统一迁移与协作锁)。

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
| `buffered_writer` | `(mode=None, max_rows=10_000, **write_kwargs) -> BufferedWriter` |
| `insert_all` | `(data, pk=None, extra_data=None, partition=None, replace=False, ignore=False, commit=True, **kwargs) -> WriteResult` |
| `upsert_all` | SQLite `(kv_list, pk, partition=None, commit=True, **kwargs)`；PG `(kv_list, pk, partition=None, commit=True, *, where=None)`（`where` 已废弃并告警，按 pk 全表合并） |
| `delete` | `(where) -> int`（返回删除行数；`where=None` 告警并转发 `delete_all()`；PG 侧 falsy 非 None 一律抛 `BadArgument`） |
| `delete_all` | `() -> int`（显式清空全表；危险操作优先 `truncate(confirm=True)`） |
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
| `select` | `(fields='*', where=None, limit=None, order_by=None, to_dict=True, columns=None, **kwargs) -> list`（默认返回 dict 行；`to_dict=False` 返回 tuple） |
| `iter_select` | `(fields='*', where=None, limit=None, order_by=None, to_dict=True, batch_size=10_000, columns=None) -> Iterator` |
| `iter_select_batches` | `(fields='*', where=None, limit=None, order_by=None, to_dict=True, batch_size=10_000, columns=None) -> Iterator[list]` |
| `iter_rows` | `(batch_size=10_000) -> Iterator[row]`（流式读取全表，逐行；PG 同签名） |
| `iter_batches` | `(batch_size=10_000) -> Iterator[list]`（SQLite / PG；流式读取全表，按批；`iter_rows` 的批量版） |
| `select_all` | 已废弃：等价于 `select(fields, where, to_dict=True)`，触发 DeprecationWarning，永久保留 |
| `explain_select` | `(fields='*', where=None, order_by=None, limit=None, columns=None, symbols=None, start=None, end=None, date=None, session=None) -> QueryPlan`（业务参数与 `query()` 对齐） |
| `latest` | `(symbols=None, as_of=None, columns='*', where=None, session=None, by=None, group_by=None) -> LatestQuery`（`group_by` 已废弃，改用 `by=`；未声明 symbol_column 且无 `by=` 时抛 `BadArgument`） |
| `asof` | 同 `latest`（别名） |

#### 诊断

| 方法 | 签名 | 返回值 |
|------|------|--------|
| `spec` | `() -> TableContract\|None` | 表契约 |
| `describe` | `() -> dict` | 表说明、主键和列类型/解释/声明值域 |
| `update_metadata` | `(*, description=省略, column_descriptions=省略, column_ranges=省略) -> dict` | SQLite/Parquet 增量更新元信息；详见下节 |
| `table_parts` | `() -> list` | 逻辑分区列表 |
| `segments` | `(include_obsolete=False) -> list[dict]` | 物理 segment 清单 |
| `validate` | `(deep=False, include_obsolete=False) -> SegmentValidationResult` | 校验 manifest 与物理文件 |
| `layout_report` | `() -> LayoutReport` | 分区诊断报告 |
| `compact` | `(partition=None, target_rows=None) -> CompactResult` | 整理 row segment |
| `cleanup_obsolete` | `(partition=None, dry_run=True, validate=True) -> CleanupResult` | 清理 obsolete segment 文件 |
| `count_rows` | `() -> int` | 统计当前可见行数（无参，各后端一致） |
| `rename` | `(new_name, exist_ok=False, overwrite=False) -> Table` | 重命名当前逻辑表 |
| `migrate_to` | `(target_uri, tables=None, mode='append', batch_size=10_000, verify=None, dry_run=False, *, specs=None, temp_dir=None, lock_timeout=30.0) -> TableMigrationResult` | 迁移单表 |
| `repartition_to` | `(target_uri, table_name=None, spec=None, mode='append', batch_size=10_000) -> RepartitionResult` | 重分区结果 |
| `create_index` | `(column, order='asc', exist_ok=True)` | 创建索引 |
| `drop_index` | `(column)` | 删除所有分区上的列索引 |

**并发语义**：写入路径（`write` / `buffered_writer` / 迁移写目标）与 `compact()` 共用 `_compact_lock` 串行化——compact 与写互斥是预期吞吐代价，换取不会向 obsolete segment 复活插入。

#### 表说明与字段元数据

`describe()` 返回 `name`、`description`、`primary_key`、`columns`；每列含 `name`、
`type`、`description`、`range`。SQLite/Parquet 支持 `update_metadata()`；PostgreSQL
读取已绑定契约中的说明，调用 `update_metadata()` 抛 `BadArgument`。

`update_metadata()` 省略的参数保留原值；`description=None` 清除表说明，
`column_descriptions=None` / `column_ranges=None` 清空对应映射，映射中单列值为 `None`
只清除该列。字段须已存在，声明值域只用于数值列，采用闭区间 `low` / `high`；
`to_dict()` / `describe()` 将无穷边界编码为 `"+inf"` / `"-inf"`。这些值域不校验写入，
也不替代数据实际的 min/max 统计。增量更新要求已有持久化 schema 元数据。

**PostgreSQL 引擎差异**：

- `delete(where)` 参数化执行并返回删除行数；`where=None` 触发 `DeprecationWarning` 并转发 `delete_all()`，`[]`/`{}`/`""`/`0`/`False` 等 falsy 非 None 值抛 `BadArgument`（不再退化为全表删除）。
- 列类型走封闭白名单（`blob→BYTEA`、`datetime→TIMESTAMP` 等）；未知类型抛 `BadArgument`，不会把用户字符串拼进 DDL。已有物理表缺契约列、或声明分区但表未分区时抛 `SchemaConflict`。
- 字符串 `fields` 只接受 `*` 或标识符列表；含 SQL 语法直接抛 `BadArgument`。`select()` / `iter_select()` / `iter_select_batches()` 拒绝未知 kwargs。
- `insert_all` 按整批行列并集取值、缺列补 `NULL`；`upsert_all` 仅更新输入字段、缺字段保留旧值，并按参数上限拆页；`commit=False` 会保持事务，后续 `commit=True` 的写入或 delete 提交，连接关闭时未提交事务回滚，失败整体回滚。
- `order_by` 的 NULL 排序与 SQLite 一致：ASC → `NULLS FIRST`，DESC → `NULLS LAST`。
- `upsert_all(..., where=...)` 的 `where` 为仅关键字参数且已废弃（忽略并告警）：upsert 以 pk 为身份通过 ON CONFLICT 增量合并输入字段。
- `calendar(...)` 的 `timestamptz` 边界带 UTC 偏移；`table_parts()` 返回排序后的子分区关系名。

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
- SQLite 有全局 `order_by` 时，为保证全局排序正确性，使用每个分区的只读游标和堆归并，
  按批惰性输出，不调用 `select()` 全量物化；`query(..., order_by=...).iter_rows()` 和
  `.iter_batches()` 也走这条路径。
- 单次有序归并同时读取的分区数受 `max_ordered_read_connections` 限制，并计入
  `max_process_connections`；超过任一预算直接抛 `BadArgument`，应收窄查询范围或调高预算。
- `limit` 会跨 batch 生效。
- `select()` / `iter_select*()` 不接受 `symbols`/`start`/`end`/`date`/`session` 或任何未知 kwargs（抛 `BadArgument`，不静默忽略）；业务过滤请用 `query()` / `explain_select()`。
- `select()` 同时给出全局 `order_by` 和 `limit` 时，`ORDER BY ... LIMIT` 会下推到每个 segment 再归并（结果不变、内存有界）；只排序不限行时仍需完整合并排序。

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

由 `table.buffered_writer(...)` 返回。上下文管理器。

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

# 工厂方法（顶层唯一导出的分区入口）
Partition.duration("ts", every="1day")
Partition.calendar("date", every="1day", timezone="Asia/Shanghai")
Partition.range("seq", every=10000)
```

> 注意：`DurationPartition`/`CalendarPartition`/`RangePartition` 类**不在顶层导出**；确需直接引用时从内部模块导入，如 `from iosql.partition.duration import DurationPartition`。

PostgreSQL native partition 下，`duration(...)` / `range(...)` 使用数值边界；
`calendar(...)` 支持 `text` / `date` / `timestamp` 目标列。text 日期列应统一
使用 ISO 日期（如 `2026-01-05`）或 compact 日期（如 `20260105`）。

### `DurationPartition`

| 参数 | 类型 | 说明 |
|------|------|------|
| `interval_ms` | `int`/`str` | 分区间隔（毫秒），也支持别名如 `"1day"` |
| `ts_col` | `str` | 时间戳列名 |

常用别名：`1min` `5min` `15min` `30min` `1hour` `4hour` `1day` `1week` `1month` `3month` `6month` `1year`

`interval_ms` 接受正整数、整数值 float 与纯数字字符串，以及 `Nunit`（含 `Nmonth`=30 天、`Nyear`=365 天）；bool/非整数 float/非正值抛 `PartitionSpecError`（`BadArgument` 子类）。`format_partition_spec()` 的输出可被 `parse_interval_ms()` 回读（月/年按固定近似往返）。

### `CalendarPartition`

| 参数 | 类型 | 说明 |
|------|------|------|
| `fmt` | `str` | 日期格式，白名单：`"YYYY"`/`"YYYYMM"`/`"YYYYMMDD"`/`"YYYYMMDDHH"`/`"YYYYMMDDHHmm"`，其他值抛 `PartitionSpecError` |
| `every` | `str`/`int` | 日历粒度（如 `"1week"`/`"4day"`），会真实参与分桶；缺省按 `fmt` 推断 |
| `tz` | `str` | 时区（如 `"Asia/Shanghai"`） |
| `ts_col` | `str` | 日期列名 |

### `RangePartition`

| 参数 | 类型 | 说明 |
|------|------|------|
| `width` | `int` | 分区宽度（正整数；`True`/小数/0 抛 `PartitionSpecError`） |
| `column` | `str` | 数值列名 |

range/duration 分区会把数值分区键向下取整归一为 `int`（`0.0` 与 `0` 是同一个分区键，等值查询可命中）。

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

约束：`max_rows >= target_rows >= min_rows > 0`。`max_rows` 必填且为正整数；`target_rows`/`min_rows` 只有 `None` 才回落默认值，显式传 `0`、bool 或非法类型抛 `BadArgument`（不会被静默替换）。`PhysicalLayout(target_rows_per_file=None)` 同样抛 `BadArgument`。

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
- `validate()` 还会反向扫描 data 目录，把未被 manifest 引用的 segment 文件以 `orphan_segment_file` 警告列出（warnings 不影响 `ok`）。
- manifest 引用的 segment 文件缺失时，读取/删除路径抛 `UnsafeOperation`（不会隐式创建空文件）；`compact()` 可安全清理占用目标文件名的孤儿文件后复用该名字。
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
| `symbol_column` | `str`/`None` | Symbol 列名；默认 `None`（显式声明后 symbols 语义才可用） |
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
| `rows_copied` | `int` | 成功交给目标写入的源行数总和，可包含 ignore 跳过的行 |
| `rows_written` | `int\|None` | 各表实际写入行数之和；无法确定或 dry-run 时为 None |
| `batches` | `int` | 总写入批次数 |
| `source_rows` | `int\|None` | 源表总行数；dry-run 或 row-count 校验时可用 |
| `target_rows` | `int\|None` | 目标表总行数；row-count 校验时可用 |
| `verify` | `str\|None` | 校验模式 |
| `verified` | `bool\|None` | 校验是否通过 |
| `dry_run` | `bool` | 是否为预览结果 |
| `published` | `bool\|None` | True 为正式发布；False 为未发布（含 dry-run）；None 为状态尚未确认 |
| `operation_id` | `str\|None` | 本次任务 ID，用于定位异常与持久状态 |

### `TableMigrationResult`

| 字段 | 类型 | 说明 |
|------|------|------|
| `table` | `str` | 源表名 |
| `target_table` | `str` | 目标表名 |
| `source_uri` | `str` | 源 URI |
| `target_uri` | `str` | 目标 URI |
| `rows_copied` | `int` | 成功交给目标写入的源行数，可包含 ignore 跳过的行 |
| `rows_written` | `int\|None` | 实际写入行数；无法确定或 dry-run 时为 None |
| `batches` | `int` | 写入批次数 |
| `source_rows` | `int\|None` | 源表行数 |
| `target_rows` | `int\|None` | 目标表行数 |
| `verify` | `str\|None` | 校验模式 |
| `verified` | `bool\|None` | 校验是否通过 |
| `dry_run` | `bool` | 是否为预览结果 |
| `published` | `bool\|None` | 正式发布状态，含义同 MigrationResult |
| `operation_id` | `str\|None` | 本次任务 ID |
| `source_spec` / `target_spec` | `dict\|None` | 源/目标契约的序列化值，dry-run 也可检查布局变化 |
| `partitions_created` | `list` | 本次新增的目标分区，不含目标既有分区 |

用 `published` 判断是否正式发布，不能用 `rows_copied` 推断提交成功。
迁移阶段失败的异常携带 `partial_result` 等进度信息；详见异常类型和
[迁移错误排查](troubleshooting.md#统一迁移与协作锁)。

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
| `PartitionSpecError` | 分区规格错误（`BadArgument` 子类；损坏/旧 manifest 缺字段也走这里） |
| `UnsafeOperation` | 不安全操作（如 truncate 未确认） |
| `QueryFieldError` | 查询字段不存在 |
| `ChunkVerificationError` | 数据块校验错误 |
| `EngineNotExist` | 不支持的存储引擎 |
| `InvalidUriSchema` | URI schema 无效 |
| `DatabaseBusy` | 迁移/恢复独占期间协作 API 立即报忙，尚未进入业务读写 |
| `MigrationError` | 迁移失败；记录任务和阶段，不代表目标已发布 |
| `MigrationVerificationError` | 行数或目标完整性校验失败；同时继承 `MigrationError` 与 `BadArgument`；目标非空的前置检查仍抛 `BadArgument` |
| `MigrationLockTimeout` | 获取源/目标库锁超过 `lock_timeout` |
| `MigrationRecoveryRequired` | 提交记录待恢复或身份无法确认，访问被恢复屏障阻止 |

异常从 `iosql.exception` 导入。`MigrationError` 提供 `operation_id`、`stage`、
`source_table`、`target_table`、`batch`、`partial_result` 和 `recovery_path`；
部分字段可为 None。迁移过程保留的其他 `IosqlError` 也可附带任务 ID、阶段、
部分结果及恢复路径。发布状态不确定时应先检查持久状态，不能直接追加重试。

---

## 可视化管理台

依赖可选 extra（fastapi + uvicorn）：

```bash
python -m pip install -e ".[ui]"
```

CLI 入口：

```bash
iosql web -s database/market.iosql [--write] [--host 127.0.0.1] [--allowed-host example.internal] [--port 8686] [--no-browser]
```

Python 入口（惰性导入，未装 fastapi 时抛带安装指引的 `ImportError`）：

```python
from iosql.web import create_app
app = create_app("sqlite://database/market.iosql", mode="readonly")  # 或 "write"
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `source` | 必填 | 数据库 URI 或本地路径 |
| `mode` | `"readonly"` | `"readonly"` 不注册任何写路由；`"write"` 开启维护操作（危险操作需预览 + 手输表名确认） |

工作区：Overview / Browse Data / Structure / Query / Charts（只读即可），Maintain 仅 write 模式。仅限本机 localhost 使用。

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
table.insert_all(list_of_dicts, pk=..., batch_size=5000)  # commit=False 显式拒绝
table.select(fields, where=None, to_dict=True)            # limit=0 按 SQL 语义返回 0 行
table.select_all(fields, where=None)
table.select_one(fields, where=None)
table.query_all(sql)
table.update(new_values, where)
table.delete(where)
table.invalidate_cache()  # 外部 DDL 后手动失效列/默认值/主键缓存

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

行为契约（PLAN-057 后）：错误路径统一先 `rollback()` 再转换异常（`transaction()` 内不重复回滚）；不可解析的 sqlite 错误消息重抛原始异常（保持 `except sqlite3.IntegrityError` 类匹配可用）；建表冲突抛类型化 `DuplicateColumn`，主键不在列中抛 `PrimaryKeyNotInColumn`。mysqlite 无内置 rename/drop_column/backup——用原生 SQL 或连接 API 完成（见 `examples/mysqlite-scenarios/`）。

行为契约（PLAN-062 后）：

- 连接池不支持 `Database(":memory:")` / `file::memory:`（抛 `BadArgument`；池内每个连接会各自打开独立内存库）。
- 写入必须显式提交：`insert()`/`executemany()` 传 `commit=False` 抛 `ValueError`；`auto_commit=False` 时不在 `transaction()` 中的直写同样被拒绝，需要事务请用 `with db.transaction():`。
- `insert_all` 按每行 key 集合分组：行中缺失的列从语句中省略，交由 SQLite 求值列 `DEFAULT`（`CURRENT_TIMESTAMP`、`(datetime('now'))` 等表达式默认值不再被当作字面量写盘）。
- `wal=False` 在未显式给出 `journal_mode` 时生效；`timeout=` 同时派生默认 `busy_timeout`（毫秒，`pragma_dict` 显式值优先）。
- 表结构缓存会在本模块 DDL（`create_table`/`add_column`/`drop_table` 等）后自动失效；通过 `db.execute()` 或外部连接执行 DDL 后需手动 `table.invalidate_cache()`。
- `drop_all_index` / `drop_column_index` 跳过主键/唯一约束的 autoindex，并用 `PRAGMA index_info` 解析复合索引/表达式索引。
- 连接池 `get_connection()` 同线程可重入；`fork()` 后子进程自动重建池状态；进程预算在全部池空闲时取声明上限的最大值（避免临时小池永久压缩全局预算）。
- `suggest_value_type()` 识别 numpy 标量（`np.int64`/`np.float32`/`np.bool_` 等）并映射到 integer/float。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `auto_commit` | `True` | 自动提交；为 `False` 时写入必须放在 `transaction()` 中或显式 `commit=True` |
| `max_connections` | `10` | 最大连接数 |
| `timeout` | `5.0` | 连接超时（秒），同时派生默认 `busy_timeout` |
| `wal` | `True` | 启用 WAL 模式（`pragma_dict` 显式给出 `journal_mode` 时以显式值为准） |

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
| `between` | — | `[min, max]` | 闭区间（内部规范形式 `between_and`，自动归一化） |

## PostgreSQL 14+ 查询与诊断补充（PLAN-078）

- `query/latest/asof` 在数据库内完成筛选和排序；start/end/as_of 均包含边界。latest 按请求 symbol 顺序输出，同时间按剩余主键升序决胜。`latest(symbols=[])` 为空，`query(symbols=[])` 保留既有无筛选行为。
- 查询对象支持 `.to_list()` / `.one()` / `.iter_rows(batch_size=..., timeout_ms=...)` / `.iter_batches(...)` / `.explain(analyze=False, raw_plan=False)`。流式对象提前关闭会释放命名游标，显式写事务不会被读取擅自提交。同一 Table 的连接不应并发消费多个流。
- PG 的 `explain_select` 追加仅关键字 analyze/raw_plan；默认不执行、隐藏谓词常量，analyze=True 提供实际耗时与 buffer，raw_plan=True 可能含业务常量。QueryPlan 追加 engine/engine_plan/planning_time_ms/execution_time_ms/index_scans；PG 分区列表使用关系名，segment 列表为空。默认 scanned_partitions 为计划关系，ANALYZE 时只记录 Actual Loops>0 的关系。
- `create_index(column=None, order='asc', exist_ok=True, *, columns=None)` 支持复合 B-tree；与主键等价时复用。`drop_index(column=None, *, columns=None)` 仅删除当前表带 iosql 标记且定义匹配的二级索引；不能按名称误删用户索引或约束索引。
- 重开物理表验证声明列类型、主键顺序、分区形态和 RANGE 单列键；分区主键必须包含分区列。
- upsert_all 按输入主键增量合并，缺字段保留旧值，显式 None 写 NULL，重复主键按输入顺序合并。count_rows 是精确计数，可能全表扫描。
- 完整示例：`examples/15_postgresql_timeseries_query.py`。百万/千万行基准：仓库 `benchmarks/benchmark_postgresql_query.py`。

迁移新增字段说明：`partitions_created` 是本次新增目标分区列表；`source_spec/target_spec` 是转换前后完整契约；`stage/source_table/target_table/batch` 在迁移错误中提供定位信息。已有 PG 绑定对象跨发布可只读重连并恢复绑定，新打开 PG 对象仍需显式 spec。
