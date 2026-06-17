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

### `Database(uri, verbose=0)`

数据库入口。支持 `sqlite://` 和 `postgresql://` URI。

| 参数 | 类型 | 说明 |
|------|------|------|
| `uri` | `str` | 数据库 URI，如 `"sqlite://db/test.iosql"` |
| `verbose` | `int` | 日志详细度，`0`=静默 |

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

#### `Database.close()`

关闭所有表和连接。

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
| `select_all` | `(fields, where, limit=None, to_dict=False, **kwargs) -> list` |
| `explain_select` | `(fields='*', where=None, order_by=None, limit=None) -> QueryPlan` |
| `latest` | `(symbols=None, as_of=None, columns='*', where=None, session=None, by=None, group_by=None) -> LatestQuery` |
| `asof` | `(symbols=None, as_of=None, columns='*', where=None, session=None, by=None, group_by=None) -> LatestQuery` |

#### 诊断

| 方法 | 签名 | 返回值 |
|------|------|--------|
| `spec` | `() -> TableContract\|None` | 表契约 |
| `table_parts` | `() -> list` | 逻辑分区列表 |
| `segments` | `() -> list[dict]` | 物理 segment 清单 |
| `layout_report` | `() -> LayoutReport` | 分区诊断报告 |
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
| `.spec()` | `QuerySpec` - 查询规格 |

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
Segment.rows(max_rows=500_000, target_rows=400_000, min_rows=300_000)
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `kind` | `"rows"` | 分段类型（当前仅支持 `"rows"`） |
| `max_rows` | `500_000` | 单文件最大行数 |
| `target_rows` | `max_rows` | 目标行数 |
| `min_rows` | `min(target_rows, max_rows)` | 最小行数 |

约束：`max_rows >= target_rows >= min_rows > 0`

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
| `IosqlBadArgument` | 参数错误（旧版别名） |

---

## 低层 mysqlite

### `iosql.utils.mysqlite.Database`

独立 SQLite 文件操作。基于文件路径（不需要 `sqlite://` 前缀）。

```python
from iosql.utils.mysqlite import Database

db = Database("path/to/data.sqlite3", auto_commit=True, max_connections=10)
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
db.get_pool_status()
db.is_pool_healthy()
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
