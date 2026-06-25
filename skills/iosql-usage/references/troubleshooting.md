# iosql 常见错误与排查指南

## 表契约错误

### TableSpecConflict

**症状**：调用 `db.table()` 或 `db.timeseries()` 时报 `TableSpecConflict`。

**原因**：表已存在，但传入的 `columns`/`primary_key`/`partition_every`/`partition`/`segment` 与已有定义不一致。

**排查**：

1. 检查是否重复声明了同名表。
2. 如需更改表结构，使用新表名或删除旧数据库目录后重建。
3. 如果只是获取已存在的表引用，使用 `db.table("name")` 而非 `db.timeseries("name", ...)`。

```python
# 正确：获取已存在的表引用
table = db.table("ticks")  # 不会报 TableSpecConflict

# 错误：尝试对已存在的表重新声明不同契约
ticks = db.timeseries("ticks", ..., columns={...})  # 可能冲突
```

### SchemaConflict vs TableSpecConflict

- `TableSpecConflict`：表级别的契约冲突（主键不同、分区策略不同、segment 配置不同）
- `SchemaConflict`：列级别的类型冲突（同名列类型不一致）

```python
# 先创建
db.table("t", pk=["id"], columns={"id": "integer", "val": "text"})

# 重新打开时列类型冲突 → SchemaConflict
db.table("t", pk=["id"], columns={"id": "integer", "val": "float"})
# Error: Column `val` type conflict: current=text new=float
```

### 主键或分区变更报 TableSpecConflict

**症状**：写入时报 `TableSpecConflict: PrimaryKeyCanNotChange` 或 `PartitionCanNotChange`。

**原因**：表已存在且元数据中记录的主键或分区策略与当前写入传入的不一致。

**排查**：

1. 使用 `table.spec()` 查看当前表契约。
2. 如需更改主键或分区策略，使用新表名或删除旧数据库目录后重建。
3. 使用 `repartition_to()` 按新策略写入新库。

### MissingPrimaryKey / MissingTimeColumn

**症状**：调用 `db.timeseries()` 时报 `MissingPrimaryKey` 或 `MissingTimeColumn`。

**原因**：`primary_key` 未包含 `time` 列，或 `columns` 中未声明 `time` 列。

**排查**：

```python
# 正确
ticks = db.timeseries(
    "ticks",
    time="ts",                        # 时间列名
    primary_key=["symbol", "ts"],     # 主键必须包含 time 列
    columns={"symbol": "text", "ts": "integer", "price": "float"},
)
```

### BadArgument

**症状**：调用 API 时参数类型或值不正确。

**常见场景**：

```python
# write() 的 rows 必须是 list/tuple/set/dict
table.write(123)  # BadArgument: write: `list/tuple/set/dict` required

# write() 必须有 TableContract
table = db.table("t")
table.write([{"x": 1}])  # BadArgument: write requires a TableContract

# write() 必须有 primary_key
table.write([{"x": 1}])  # MissingPrimaryKey: write requires TableContract.primary_key

# 不支持的模式
table.write(data, mode="merge")  # BadArgument: Unsupported write mode

# spec 与 shortcut 参数冲突
db.table("t", spec=spec, pk=["id"])  # BadArgument: spec conflicts with shortcut arguments

# pk 与 primary_key 冲突
db.table("t", pk=["a"], primary_key=["b"])  # BadArgument: pk and primary_key conflict

# repartition_to 目标不能与源相同
table.repartition_to(source_uri)  # BadArgument: requires a different target uri or table

# limit 不能为负数
table.select("*", where=None, limit=-1)  # BadArgument: limit must be non-negative
```

### PartitionSpecError

**症状**：分区参数不正确。

**排查**：

```python
# 不支持的时间单位
TimeColumn("ts", unit="nanoseconds")  # PartitionSpecError

# 不支持的日历分区粒度
Partition.calendar("date", every="1week")  # PartitionSpecError

# 无效的分区时长
Partition.duration("ts", every=-1)  # PartitionSpecError
```

### UnsafeOperation

**症状**：执行危险操作时未显式确认。

```python
table.truncate()  # UnsafeOperation: truncate requires confirm=True

# 正确
table.truncate(confirm=True)
```

## 查询错误

### 分区未按预期裁剪

**症状**：`explain()` 显示扫描的分区数多于预期。

**排查**：

1. 确认 `start`/`end` 参数的时间单位与时区与数据一致。
2. 对于 duration 分区，确认 `start` 在分区边界之前，`end` 在分区边界之后。
3. 使用 `explain()` 或 `explain_select()` 检查 `scanned_partitions` 和 `skip_reasons` 字段：

```python
plan = table.query(symbols="AAPL", start=1700000000000, end=1710000000000).explain()
print("扫描:", plan.scanned_partitions)
print("跳过原因:", plan.skip_reasons)
```

4. 使用 `explain_select()` 可同时查看 segment 级的裁剪：

```python
plan = table.explain_select("*", where=where, order_by="ts")
print("跳过 segment:", plan.segment_skip_reasons)
```

5. 确认没有拼错列名导致时间条件未被识别。

### QueryFieldError

**症状**：`query(where="nonexistent = 1")` 报 `QueryFieldError`。

**原因**：where 条件中引用了不存在的列。

**排查**：只使用 `columns` 中声明的列作为 where 条件。

### 查询结果在 Python 层排序较慢

**症状**：多分区查询延迟高，特别是带 `order_by` 和 `limit` 时。

**原因**：多分区查询的 `order_by` 和 `limit` 在 Python 层合并排序，数据量大时性能受影响。

**排查**：

1. 尽量通过 time 条件收窄扫描范围。
2. 检查 `plan.requires_global_sort` 确认是否触发全局排序。
3. 检查 `plan.requires_python_limit` 确认 limit 是否在 Python 层执行。

## 写入错误

### 主键冲突导致写入静默跳过

**症状**：`write(mode="ignore")` 后行数少于预期，但没有报错。

**排查**：

1. 检查 `WriteResult.rows_seen` vs `WriteResult.rows_written`。
2. 如果预期覆盖，非 segment 表可显式使用 `mode="upsert"`。
3. 如果是 `Segment.rows` 表，当前不要依赖覆盖写；改用 append/ignore 或等待 pk index/tombstone 设计。

```python
result = table.write(data)
print(f"seen={result.rows_seen}, written={result.rows_written}")
# 如果 rows_written < rows_seen，说明有行被跳过
```

### 写入后查询不到数据

**症状**：`write()` 成功但 `query()` 查不到刚写入的数据。

**排查**：

1. 确认查询的 `start`/`end` 范围覆盖了数据的时间戳。
2. 确认没有将毫秒时间戳当作秒级时间戳使用。
3. 检查是否显式使用了 `mode="upsert"` 或表契约 `conflict="upsert"`，导致同主键数据被覆盖。
4. 使用 `table.segments()` 确认数据已写入正确的分区文件。
5. 使用 `table.table_parts()` 确认分区是否已创建。

### Segment 相关写入问题

**症状**：开启 segment 后，跨 segment 的 upsert 未生效。

**原因**：当前 `Segment.rows` 是 append-first 实现，未实现全局 pk index/tombstone，因此不允许 `upsert/replace` 覆盖写。

**排查**：

1. 使用 `table.segments()` 检查哪些 segment 是 sealed 状态。
2. 晚到数据会进入新的 active segment，查询时可能扫描多个重叠 segment。
3. 纯新增数据场景使用默认 append 性能最优。
4. 如果晚到数据导致同一范围扫描多个 segment，可显式执行 `table.compact()` 重写当前可见 segment。
5. 需要覆盖写时，暂时关闭 segment 或先离线重分区。

### WriteResult 返回 layout_warnings

**症状**：`WriteResult.layout_warnings` 不为空。

**排查**：

```python
result = table.write(data)
if result.layout_warnings:
    for warning in result.layout_warnings:
        print(f"布局警告: {warning}")
    # 考虑调用 layout_report() 检查分区状态
```

## 目录与 URI 错误

### InvalidUriSchema

**症状**：`Database("data.sqlite3")` 报 `InvalidUriSchema`。

**原因**：主 API 要求 `sqlite://` 或 `postgresql://` 前缀的 URI 格式。

**排查**：

```python
# 正确：SQLite 使用 sqlite:// 前缀
db = Database("sqlite://db/market.iosql")

# 正确：PostgreSQL 使用 postgresql:// 前缀
db = Database("postgresql://user:pass@localhost:5432/mydb")

# 错误：直接传文件路径（低层 mysqlite 才接受）
db = Database("db/market.iosql")  # 报 InvalidUriSchema
```

### EngineNotExist

**症状**：URI schema 不是 `sqlite`/`sqlite3`/`postgresql`。

```python
db = Database("mysql://localhost/db")  # EngineNotExist
```

### 找不到已存在的数据库

**症状**：`db.table("name")` 报错，表不存在。

**排查**：

1. 确认 `sqlite://` 后的路径正确，目录存在。
2. 使用 `TableSpec` 显式声明时确认传入 `spec=spec`。
3. 首次使用需要 `db.timeseries("name", ...)` 或 `db.table("name", pk=..., ...)` 创建表。
4. 使用 `Database.table_names()` 查看数据库中已存在的表。

## SQLite 到 PostgreSQL 迁移

### 缺少 psycopg2

**症状**：迁移到 PostgreSQL 时报 `ModuleNotFoundError: psycopg2`。

**排查**：

```bash
python -m pip install "iosql[postgresql]"
```

### migrate_to 找不到表契约

**症状**：`migrate_to requires a TableContract`。

**原因**：源表不是通过 `Database.table(..., pk=..., columns=...)` 或 `timeseries(...)` 创建，缺少持久化契约。

**排查**：

1. 使用 `source.table("name").spec()` 检查源表契约。
2. 对无契约的历史表，先用 `Database.table("name", spec=...)` 声明兼容契约。
3. 再执行 `source.migrate_to("postgresql://...")`。

### PostgreSQL 迁移太慢或内存压力大

**说明**：`migrate_to()` 会按 `batch_size` 从 SQLite 物理文件逐批读取，不会一次性载入全库。海量 K 线建议从 `50_000` 行批次开始测试。

```python
source.to_postgresql(
    "postgresql://user:pass@localhost:5432/market",
    batch_size=50_000,
)
```

CLI：

```bash
iosql migrate -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --batch-size 50000
```

### row-count 校验失败

**症状**：`verify="row-count"` 或 `iosql migrate --verify row-count` 抛出
`row-count verification failed`。

**常见原因**：

1. 目标表在迁移前已有旧数据。
2. 使用 `ignore` / `upsert` 导致目标实际新增行数少于源行数。
3. 迁移期间源 SQLite 库仍有写入，源行数发生变化。

**排查**：

1. 先执行 `iosql migrate ... --dry-run` 确认源表行数和批次数。
2. 对全量迁移使用空目标表或独立目标表名。
3. 迁移窗口内暂停源库写入，再执行 `--verify row-count`。

## 分区维护

### repartition_to 后原库未变化

**症状**：调用 `repartition_to()` 后原库分区布局未改变。

**说明**：这是预期行为。`repartition_to()` 写入新的目标库，不会原地修改源库。

```python
# repartition_to 创建新库
result = source_table.repartition_to("sqlite://new_db.iosql", spec=new_spec)
print(result.rows_copied)  # 新库中的行数
# 原库的 source_table 不受影响
```

### compact 后仍看到 obsolete segment

**症状**：`table.compact()` 后，`segments(include_obsolete=True)` 仍显示旧 segment，磁盘文件也还存在。

**说明**：这是预期行为。compact 会在原库内写入新 generation，并把旧 segment 标记为 `obsolete`。默认查询、`segments()`、`layout_report()` 不再使用 obsolete segment；`compact()` 本身不会物理删除旧 SQLite 文件。

**排查**：

1. 使用 `table.segments()` 查看当前可见 segment。
2. 使用 `table.segments(include_obsolete=True)` 审计旧 segment 状态。
3. 对结果敏感的数据先比较 compact 前后的查询结果。
4. 使用 `table.validate(deep=True)` 或 `iosql validate --deep` 校验当前可见 segment。
5. 使用 `table.cleanup_obsolete()` 或 `iosql cleanup-obsolete` 预览可释放空间。
6. 确认校验通过后再执行 `table.cleanup_obsolete(dry_run=False)` 或 `iosql cleanup-obsolete --yes`。
7. 不要把 compact 理解为 `VACUUM`；它整理 iosql segment 布局，释放磁盘需要显式 cleanup。

### cleanup-obsolete 拒绝删除

**症状**：`cleanup_obsolete(dry_run=False)` 或 `iosql cleanup-obsolete --yes` 抛出校验失败。

**排查**：

1. 先运行 `table.validate(deep=True).to_dict()` 或 `iosql validate -s DB -t TABLE --deep` 查看具体错误。
2. 如果是 `row_count_mismatch`、`time_min_mismatch`、`stats_min_mismatch` 等错误，说明当前可见 segment 的 manifest 与物理文件不一致，不应删除 obsolete 文件。
3. 如果只是想预览可释放空间，使用默认 dry-run，不要传 `--yes`。
4. 只有确认当前可见 segment 可被查询且校验问题已修复后，才重新执行清理。

### layout_report 的 recommendation 误解

**症状**：`layout_report().recommendation` 显示 "repartition" 但不确定含义。

**排查**：

1. 检查 `report.total_rows` 和 `report.partitions`。
2. 检查 `report.oversized_partitions` 和 `report.undersized_partitions`。
3. `recommendation` 是基于行数/分区数阈值的建议，不是必须执行。
4. 需要重分区时使用 `repartition_to()`。

### Segment 配置不生效

**症状**：配置了 `Segment.rows(max_rows=N)` 但文件没有自动滚动。

**排查**：

1. 确认数据量超过 `max_rows`。
2. 注意 `target_rows`、`min_rows`、`max_rows` 三级阈值的关系：
   - `max_rows >= target_rows >= min_rows > 0`
3. append-only 模式下，已有 segment 不会自动拆分；新数据超过 `max_rows` 时才会创建新 segment。

## 低层 mysqlite 问题

### 连接池不健康

**症状**：高并发或多线程场景下偶尔出现连接错误。

**排查**：

```python
from iosql.utils.mysqlite import Database

db = Database("data.sqlite3")
# 检查连接池状态
status = db.get_connection_pool_status()
print(status)  # 查看活跃/闲置连接数

# 检查健康度
if not db.is_connection_pool_healthy():
    db.close()
    db = Database("data.sqlite3")  # 重新创建
```

### 动态列不生效

**症状**：`insert(ensure_column=True)` 后新列未出现。

**排查**：

1. 确认调用了 `ensure_column=True`。
2. 对于时序表，列必须在 `TableSpec` 中声明，动态加列仅在 mysqlite 低层模块中支持。

### Too Many Open Files

**症状**：`OSError: [Errno 24] Too many open files`。

**排查**：

1. 主 API 显式设置 `Database(..., max_open_partitions=5, max_connections_per_file=1, max_process_connections=32)`。
2. `max_open_partitions` 是单表分区数据库缓存上限，分区按 LRU 自动关闭。
3. `max_connections_per_file` 是每个 SQLite 文件的连接池上限；WAL 模式下每条连接
   可能对应数据库、WAL、SHM 三个文件描述符。
4. `max_process_connections` 是跨数据库、跨表共享的真实连接预算；预算满时自动回收
   其他池的空闲连接。
5. 使用上下文管理器或显式 `db.close()`，不要依赖进程退出或垃圾回收。
6. 运行 `python benchmarks/stress_open_files.py --root <目标盘>`，检查报告中的
   `fd_peak`、`fd_final`、`peak_process_connections` 和 `peak_open_partitions`。
7. 不要直接修改 `_num_opened_max_allowed` 私有字段，也不要把提高 `ulimit -n`
   当作库内资源泄漏的解决方案。

## 测试相关

### 测试间数据污染

**症状**：测试 A 的数据影响测试 B 的结果。

**排查**：

```python
# 每个测试使用独立临时目录
import tempfile

def test_my_case():
    tmp_path = tempfile.mkdtemp()
    db = Database(f"sqlite://{tmp_path}/test.iosql")
    # ... test logic ...
    db.close()
```

### pytest 运行建议

```bash
# 仅跑 sqlite 相关测试
pytest tests/engines/sqlite/ -v

# 跑特定测试文件
pytest tests/engines/sqlite/test_timeseries_contract.py -v

# 跑分区测试
pytest tests/test_partition.py -v
pytest tests/test_partition_integration.py -v

# 含输出
pytest tests/engines/sqlite/ -v -s --tb=short
```
