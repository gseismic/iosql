# iosql 使用模式

## Partition 与 Segment：四种组合

`partition`（逻辑分区）和 `segment`（物理分段）是 iosql 核心的二级文件管理：

| 组合 | 数据路由 | 文件管理 | 典型场景 |
|------|---------|---------|---------|
| partition + segment | 按分区键 | 分区内按行数滚动 | 高频 tick/K 线 |
| 仅 partition | 按分区键 | 每分区单文件 | 日线、财报 |
| 仅 segment | 全部到 `__all__` | 按行数滚动 | 日志、事件流 |
| 两者皆无 | 无路由 | 单文件 | KV 表、元数据 |

目录结构：

```text
# partition + segment（完整模式）
ticks/data/
├── 1735689600000.sqlite3          # partition=A, segment=0
├── 1735689600000-1.sqlite3        # partition=A, segment=1  行数满了
└── 1735776000000.sqlite3          # partition=B, segment=0

# 仅 segment（无 partition）
events/data/
├── __all__-0.sqlite3              # segment=0
├── __all__-1.sqlite3              # segment=1
└── __all__-2.sqlite3              # segment=2

# 仅 partition（无 segment）  
daily_bars/data/
├── 1735689600000.sqlite3          # 单文件，无限增长
└── 1735776000000.sqlite3

# 两者皆无  
simple/data/
└── simple.sqlite3                 # 单文件
```

以下按组合方式提供完整代码示例。

## 重命名 database/table

```python
from iosql import Database

db = Database("sqlite://db/market.iosql")

# 重命名 SQLite 目录库；当前 db.uri 会刷新到新路径。
db.rename("market-renamed.iosql")

# 重命名逻辑表；SQLite 会移动表目录并更新 manifest。
table = db.rename_table("ticks", "ticks_2026")
table.rename("ticks")

db.close()
```

CLI：

```bash
iosql rename-database -s db/market.iosql --target market-renamed.iosql
iosql rename-table -s db/market.iosql -t ticks --new-table ticks_2026
```

覆盖已有目标需要显式确认：Python 传 `overwrite=True`；CLI 传
`--overwrite --yes`。`Database.rename` 会校验源/目标身份并拒绝根目录、家目录、
符号链接目标与互相包含的路径，覆盖失败可回滚。

## 毫秒时序表：原始 Table 版

```python
from iosql import Database, Partition, Segment, TableSpec, TimeColumn

db = Database(f"sqlite://{db_path}")
spec = TableSpec(
    pk=["symbol", "ts"],
    time=TimeColumn("ts", unit="ms", timezone="UTC"),
    partition=PartitionSpec.by_time("ts", every="1day"),
    columns={"symbol": "text", "ts": "integer", "price": "float"},
)
ticks = db.table("ticks", spec=spec)

ticks.write([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
])

rows = ticks.query(
    symbols=["AAPL", "MSFT"],
    start=1735689600000,
    end=1735776000000,
    order_by="ts",
).to_list()

plan = ticks.query(start=1735689600000, end=1735776000000).explain()
db.close()
```

`time` 是业务时间语义，供 `query(start/end/date)`、`latest()` 和 `asof()` 解释输入；`partition` 是物理布局，供写入落盘和查询裁剪使用。

## 毫秒时序表：快捷版（推荐新项目）

```python
from iosql import Database, Partition, Segment

db = Database(f"sqlite://{db_path}")
ticks = db.table(
    "ticks",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition=Partition.duration("ts", every="1day"),
    segment=Segment.rows(max_rows=500_000),
    order_key="ts",
)

ticks.write([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
])

where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between", "value": [1735689600000, 1735776000000]},
]
rows = ticks.select("*", where=where, order_by="ts", to_dict=True)
plan = ticks.explain_select("*", where=where, order_by="ts")
db.close()
```

## 毫秒时序表：timeseries 快捷版

```python
from iosql import Database

db = Database(f"sqlite://{db_path}")
ticks = db.timeseries(
    "ticks",
    time="ts",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1day",
)

ticks.write([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
])

rows = ticks.query(
    symbols=["AAPL", "MSFT"],
    start=1735689600000,
    end=1735776000000,
    order_by="ts",
).to_list()

plan = ticks.query(start=1735689600000, end=1735776000000).explain()
db.close()
```

## 日期字符串和日历分区：原始 Table 版

```python
events = db.table("events", spec=TableSpec(
    pk=["event_date", "name"],
    time=TimeColumn("event_date", unit="date", timezone="Asia/Shanghai"),
    partition=PartitionSpec.by_time(
        "event_date",
        every="1day",
        kind="calendar",
    ),
    columns={"event_date": "text", "name": "text", "city": "text"},
))

events.write({"event_date": "2026-01-06", "name": "review", "city": "Beijing"})
rows = events.query(date="2026-01-06").to_list()
```

`timeseries()` 快捷版可用 `time_unit="date"` 和 `partition_kind="calendar"` 表达同等契约。

## 数值 Range 分区（非时间字段）

适用于按序号、offset、版本号等任意可排序数值列分区：

```python
from iosql import Database, Partition

db = Database("sqlite://db/sequence.iosql")
events = db.table(
    "events",
    pk=["event_id"],
    columns={"event_id": "integer", "seq": "integer", "kind": "text"},
    partition=Partition.range("seq", every=10000),
    order_key="seq",
)

events.write([
    {"event_id": 1, "seq": 500, "kind": "click"},
    {"event_id": 2, "seq": 15000, "kind": "scroll"},
])

rows = events.select(
    "*",
    where={"field": "seq", "op": "between", "value": [0, 20000]},
    order_by="seq",
    to_dict=True,
)
db.close()
```

Range 分区使用 `(value // width) * width` 取整作为分区键，支持任意整数字段。

## 纯 Row Segment（无逻辑分区）

不配置 `partition`，仅用 `segment` 按行数自动拆分物理文件。所有数据共享 `DEFAULT_LOGICAL_PART`（`"__all__"`）：

```python
from iosql import Database, Segment, DEFAULT_LOGICAL_PART

db = Database("sqlite://db/events.iosql")
events = db.table(
    "events",
    pk=["event_id"],
    columns={"event_id": "integer", "seq": "integer", "kind": "text"},
    segment=Segment.rows(max_rows=3, target_rows=2, min_rows=1),
    order_key="seq",
)

# 写入超过 max_rows 时自动滚动到新 segment
events.write([
    {"event_id": i, "seq": i, "kind": "base"}
    for i in range(6)
])

# 晚到数据会进入新的 active segment
events.write({"event_id": 99, "seq": 1, "kind": "late"})

print("逻辑分区:", events.table_parts())  # ['__all__']
print("segments:", events.segments())

# explain 可查看二级裁剪
plan = events.explain_select(
    "*",
    where={"field": "seq", "op": "between", "value": [0, 3]},
)
print("扫描:", plan.scanned_segments)
print("跳过:", plan.segment_skip_reasons)
db.close()
```

## 缓冲批量写入（buffered_writer）

适合大批量数据摄入，自动分批 flush：

```python
base_ts = 1735689600000
with ticks.buffered_writer(max_rows=4) as buf:
    for hour in range(100):
        for symbol in ["AAPL", "MSFT", "GOOGL"]:
            buf.append({
                "symbol": symbol,
                "ts": base_ts + hour * 3_600_000,
                "price": 100 + hour * 0.5,
            })
# 退出上下文时自动 flush 剩余数据
```

## 交易日/session

```python
futures = db.timeseries(
    "futures_ticks",
    time="ts",
    pk=["symbol", "ts"],
    columns={
        "symbol": "text",
        "ts": "integer",
        "trading_date": "text",
        "price": "float",
    },
    session_column="trading_date",
)

session_rows = futures.query(session="2026-01-06", symbols="AU").to_list()
snapshot = futures.latest(
    session="2026-01-06",
    symbols="AU",
    as_of=1767661200000,
).one()
```

## 多维 latest/asof

```python
bars = db.timeseries(
    "bars",
    time="ts",
    pk=["symbol", "timeframe", "ts"],
    columns={
        "symbol": "text",
        "timeframe": "text",
        "ts": "integer",
        "close": "float",
    },
)

latest = bars.latest(
    symbols=["AAPL", "MSFT"],
    as_of=1735776000000,
    by=["symbol", "timeframe"],
    columns=["symbol", "timeframe", "ts", "close"],
).to_list()
```

`by` 支持多列分组（`group_by=` 已废弃，请统一使用 `by=`）。不传 `by` 时默认按 `symbol_column` 分组；`db.table()` 默认 `symbol_column=None`——未显式声明（或用 timeseries() 默认 `"symbol"`）时，`latest(symbols=...)` / `query(symbols=...)` 会抛带修复指引的 `BadArgument`。

## 写入模式

- `mode=None`：使用表契约 `conflict`，新表默认 `append`。
- `mode="append"`：严格追加，重复主键应抛出数据库约束错误。
- `mode="ignore"`：同主键已有行时跳过。
- `mode="upsert"`：同主键覆盖旧行，当前不支持 `Segment.rows` 表。
- `mode="replace"`：显式 replace，同主键覆盖旧行，当前不支持 `Segment.rows` 表。
- 每行必须包含完整主键字段；缺失主键列会抛 `BadArgument`，不会退回 SQLite rowid 自增。

```python
result = table.write(data)
print(f"seen={result.rows_seen}, written={result.rows_written}")
```

## upsert_all：字段级合并写入

`upsert_all` 按主键合并字段：已存在的行被更新（新值覆盖旧值），不存在的行被插入：

```python
# 初始数据
table.write([
    {"id": 1, "name": "Alice", "score": 80},
    {"id": 2, "name": "Bob", "score": 90},
])

# 合并更新：只传需要更新的字段
table.upsert_all([
    {"id": 1, "score": 85},          # Alice 的 score 更新为 85，name 保留
    {"id": 3, "name": "Charlie"},    # 新行插入
], pk=["id"])

# 结果：id=1(name=Alice,score=85), id=2(name=Bob,score=90), id=3(name=Charlie)
```

## delete 和 truncate

```python
# 条件删除（返回删除行数）
deleted = ticks.delete(where={"field": "symbol", "op": "==", "value": "DELISTED"})
print(f"删除了 {deleted} 行")

# 显式清空全表（Python 专用；delete(None) 已废弃并转发到这里）
ticks.delete_all()

# 清空表（需显式确认）
ticks.truncate(confirm=True)
```

`delete` 的 `where` 必须是合法条件：`{}`/`0`/`False` 等非法值抛 `BadArgument`
（PG 侧连 `[]`/`0`/`False` 都拒绝，防止误判为全表删除）。全表清空请显式用
`delete_all()` 或 `truncate(confirm=True)`。

## create_index

为所有分区的数据表创建索引：

```python
# 在 ticks 表的 symbol 列上创建索引
ticks.create_index("symbol", order="asc", exist_ok=True)
```

索引在底层 SQLite 分区文件中创建，通过遍历所有 `_physical_part_refs()` 执行。

## 查询计划深度分析（explain）

`explain_select` 返回完整的 `QueryPlan`，包含二级裁剪信息：

```python
plan = ticks.explain_select(
    "*",
    where=[
        {"field": "symbol", "op": "in", "value": ["AAPL"]},
        {"field": "ts", "op": "between", "value": [start_ts, end_ts]},
    ],
    order_by="ts",
    limit=100,
)

print("候选逻辑分区:", plan.candidate_partitions)
print("实际扫描分区:", plan.scanned_partitions)
print("跳过分区:", plan.skipped_partitions)
print("跳过原因:", plan.skip_reasons)

print("候选 segment:", plan.candidate_segments)
print("实际扫描 segment:", plan.scanned_segments)
print("跳过 segment:", plan.skipped_segments)
print("segment 跳过原因:", plan.segment_skip_reasons)

print("需要全局排序:", plan.requires_global_sort)
print("需要 Python 层 limit:", plan.requires_python_limit)
```

## 流式查询和导出

大结果集不要优先使用 `select()` / `to_list()` 一次性加载。无全局排序时使用批量迭代：

```python
where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between", "value": [start, end]},
]

for batch in ticks.iter_select_batches(
    "*",
    where=where,
    batch_size=50_000,
    to_dict=True,
):
    process(batch)

for batch in ticks.query(start=start, end=end).iter_batches(batch_size=50_000):
    process(batch)
```

CLI 导出会批处理写 JSONL/CSV/JSON：

```bash
iosql export \
  -s sqlite://db/market.iosql \
  -t ticks \
  -o ticks.jsonl \
  --where '{"field":"ts","op":">=","value":1735689600000}' \
  --batch-size 50000
```

带全局 `order_by` 的跨 segment 查询为了保证排序正确性，需要归并各 segment 的有序流；
`iter_select*()` 和 `query(..., order_by=...).iter_rows()/iter_batches()` 使用每个分区的
只读游标和堆归并，按批惰性输出，不先调用 `select()` 全量物化。单次归并受
`max_ordered_read_connections` 与 `max_process_connections` 约束，超限会抛 `BadArgument`；
大结果集示例见 `examples/13_streaming_ordered_query.py`。`select()` 在 `order_by` 与
`limit` 同时给出时会把 `ORDER BY ... LIMIT` 下推到每个 segment 再归并（结果不变、内存有界）。
`select()` / `iter_select*()` 不接受 `symbols/start/end/date/session` 等业务参数（抛
`BadArgument`），业务过滤请用 `query()` / `explain_select()`。

CLI 导入会批处理读取 JSONL/CSV 并写入既有契约表：

```bash
iosql import \
  -s sqlite://db/market.iosql \
  -t ticks \
  -f ticks.jsonl \
  --format jsonl \
  --batch-size 50000 \
  --mode append
```

CSV 字段会按表契约 `columns` 做基础类型转换。JSON 对象或对象数组保持兼容，但大文件推荐 JSONL/CSV。

## 表契约检查与获取

```python
# 获取当前表契约（含已持久化的元数据）
spec = table.spec()
print(spec.primary_key, spec.columns, spec.partition)

# 获取逻辑分区列表
parts = table.table_parts()
print(f"分区数: {len(parts)}")

# 获取物理 segment 清单
segments = table.segments()
for seg in segments:
    print(seg["part_key"], seg["row_count"], seg["sealed"], seg["state"])
```

## 表说明与字段元数据

给已有、已持久化 schema 的 SQLite/Parquet 表补充业务说明：

```python
info = ticks.update_metadata(
    description="逐笔行情，UTC 毫秒时间戳。",
    column_descriptions={"ts": "事件时间。", "price": "成交价。"},
    column_ranges={"price": {"low": 0, "high": float("inf")}},
)
print(ticks.describe())
```

建表也可传 `description` / `column_descriptions` / `column_ranges`。值域为声明的
闭区间，不校验写入或参与 min/max 统计；返回值将无穷边界编码为 `"+inf"` / `"-inf"`。
更新时省略的参数保持原值，映射中传 `{"price": None}` 清除该列条目，整个参数为 None
清空该项。PostgreSQL 支持读取绑定契约的 `describe()`，不支持 `update_metadata()`。
精确清除规则见 [API 参考](api-reference.md#表说明与字段元数据)。

## Where 条件构造

### Dict 格式（推荐）

```python
# 单条件
where = {"field": "symbol", "op": "==", "value": "AAPL"}

# 列表 → AND 组合
where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between", "value": [start, end]},
]

# 嵌套列表 → OR 组合
where = [
    [{"field": "symbol", "op": "==", "value": "AAPL"}],
    [{"field": "symbol", "op": "==", "value": "MSFT"}],
]
```

### 文本表达式（有限支持）

受支持的文本 where 表达比较、`and`/`or`、括号、`between` 和 `in`：

```python
where = "symbol in ('AAPL', 'MSFT') and ts between 1735689600000 and 1735776000000"
```

文本表达式会被解析为内部条件，不会作为原始 SQL 拼接。

### Case 和 CaseExpression（高级）

手动构造复杂条件树：

```python
from iosql import Case, CaseExpression

c1 = Case("symbol", "==", "AAPL")
c2 = Case("ts", "between", [start, end])

# AND 组合
expr = c1 * c2  # 等价于 CaseExpression('and', c1, c2)

# OR 组合
expr_or = c1 + c2  # 等价于 CaseExpression('or', c1, c2)

# 传给 select
rows = ticks.select("*", where=expr, to_dict=True)
```

## 布局和重分区

```python
report = table.layout_report()
segments = table.segments()

# 查看报告详情
print(f"总计 {report.total_rows} 行, {report.total_partitions} 个分区")
print(f"超大分区: {report.oversized_partitions}")
print(f"过小分区: {report.undersized_partitions}")
print(f"建议: {report.recommendation}")

# 整理 row segment：适合晚到数据导致 stats 范围重叠、查询扫描 segment 变多时显式执行
compact_result = table.compact()
print(compact_result.to_dict())

# 深度校验当前可见 segment，适合清理 obsolete 前执行
validation = table.validate(deep=True)
print(validation.to_dict())

# 默认 dry-run；确认 validation.ok 后再 dry_run=False
preview = table.cleanup_obsolete()
print(preview.to_dict())

# 默认只看当前可见 segment；需要审计旧文件时包含 obsolete
all_segments = table.segments(include_obsolete=True)
print([seg["state"] for seg in all_segments])

# 重分区
new_spec = table.spec().with_partition(every="1hour")
result = table.repartition_to(
    f"sqlite://{target_path}",
    spec=new_spec,
)
```

`layout_report()` 只做诊断。`compact()` 在原库内生成新的 sealed generation，并把旧 segment 标记为 `obsolete`，但不直接物理删除旧文件。需要释放磁盘时先 `validate(deep=True)`，再 `cleanup_obsolete(dry_run=False)`。`repartition_to()` 写新库，适合离线迁移和验证，不要假设它会原地修改源库。

## 已有库继续写入

重新打开已有库时，不要再次声明一套可能冲突的契约。直接按表名读取持久化
`TableSpec`：

```python
db = Database("sqlite://db/market.iosql")
if "ticks" not in db.table_names():
    raise RuntimeError("ticks 不存在")

ticks = db.table("ticks")
print(ticks.spec())
ticks.write(new_rows, mode="ignore")
db.close()
```

已有库重分区同样先读取原契约，再生成新契约并写到不同 URI：

```python
source_db = Database("sqlite://db/market.iosql")
source = source_db.table("ticks")
target_spec = source.spec().with_partition(every="1hour")
result = source.repartition_to(
    "sqlite://db/market-hourly.iosql",
    spec=target_spec,
    batch_size=50_000,
)
source_db.close()
```

完整且互相独立的示例见 `09_existing_database_append.py` 和
`10_existing_database_repartition.py`。默认模式会先准备并关闭各自的示例源库，
再以“已有库”方式重新打开；设置环境变量后可指向真实库，且不会清理外部源库。

## 另存副本并比较 engine

`save_as()` 与 `migrate_to()` 都保留源库，前者更直接表达“另存一份”的意图。
库对象默认复制全库；表对象默认只复制当前表，参数与结果类型沿用迁移接口。

```python
with Database("sqlite://db/market.iosql") as source:
    result = source.save_as("parquet://db/comparison.iosql", verify="row-count")
    assert result.published and result.verified
```

副本与源库后续写入互不影响，源对象仍指向原库。目标已有数据时按 `mode` 处理主键冲突，
不自动清空。比较完整数据集时选择新目标并开启行数校验，再使用相同查询条件、投影、
排序和消费方式计时。复制、打开库、校验的时间与查询时间分开；区分首次查询和预热重复查询，
首次查询不意味着系统冷缓存。可运行示例：[18_save_as_compare_engines.py](../examples/18_save_as_compare_engines.py)。

## SQLite 与 Parquet 迁移

SQLite 与 Parquet 目录库可以通过同一迁移接口互转。先用 dry-run 查看表、源行数和批次数，
再使用 `verify="row-count"` 校验目标行数：

```python
with Database("sqlite://db/market.iosql") as source:
    preview = source.migrate_to(
        "parquet://db/archive.iosql",
        dry_run=True,
        verify="row-count",
        batch_size=50_000,
    )
    result = source.migrate_to(
        "parquet://db/archive.iosql",
        verify="row-count",
        batch_size=50_000,
    )
```

Parquet → SQLite 的方向只需把源和目标 URI 对调。CLI 用 `iosql migrate -s SOURCE --target TARGET`；
Parquet URI 显式使用 `parquet://`；DuckDB 随 iosql 默认依赖安装，无需 extra。
文件重写成本、流式读取生命周期和其他后端边界见 [Parquet 使用指南](parquet.md)。

正式迁移持有源/目标独占租约，协作 API 在此期间报 `DatabaseBusy`。
迁移前关闭读取流、flush 缓冲并退出缓冲写入上下文；行数校验要求选中目标表不存在或为空。
dry-run 不连接目标，不验证目标权限或已有表兼容性。

## SQLite 迁移到 PostgreSQL

已有 SQLite 目录库迁移到 PostgreSQL 时，优先使用高层迁移接口。它会复用源表契约，
并按 `batch_size` 从 SQLite 物理文件逐批读取，适合海量 K 线：

```python
with Database("sqlite://db/market.iosql") as source:
    target_uri = "postgresql://user:pass@localhost:5432/market"
    options = {
        "tables": {"kline_1m": "pg_kline_1m"},
        "batch_size": 50_000,
        "verify": "row-count",
    }
    plan = source.migrate_to(target_uri, dry_run=True, **options)
    print(plan.source_rows, plan.batches)  # 不连接目标库
    result = source.migrate_to(target_uri, **options)
    print(result.published, result.rows_written, result.verified)
```

单表迁移：

```python
source = Database("sqlite://db/market.iosql")
ticks = source.table("ticks")
result = ticks.migrate_to(
    "postgresql://user:pass@localhost:5432/market",
    tables={"ticks": "pg_ticks"},
    batch_size=50_000,
)
source.close()
```

CLI：

```bash
iosql migrate \
  -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --table kline_1m:pg_kline_1m \
  --batch-size 50000 \
  --verify row-count
```

使用 `iosql migrate ... --dry-run` 可先输出表、源行数和批次数估算。

生产迁移建议：

1. 先确认 `source.table("name").spec()` 不为 `None`。
2. 先 dry-run，确认源行数、目标表名和批次数。
3. 使用空目标表或新的目标表名。
4. 全量迁移开启 `verify="row-count"`。
5. 关闭读取流、flush 缓冲、退出缓冲写入上下文并结束未提交事务；库级独占租约负责阻止协作读写，原始文件访问和旧版客户端需另行停用。
6. 预留暂存和完整目标视图的磁盘空间，检查 `published`；异常时保留 `operation_id`、`partial_result` 与 `recovery_path`，确认发布状态后再重试。

PostgreSQL 也可以作为迁移源写回 SQLite。重新打开 PostgreSQL 逻辑表时，需要绑定
同一份表契约：

```python
spec = sqlite_source.table("ticks").spec()

pg = Database("postgresql://user:pass@localhost:5432/market")
pg.table("pg_ticks", spec=spec)
result = pg.migrate_to(
    "sqlite://db/from-postgresql.iosql",
    tables={"pg_ticks": "ticks"},
    verify="row-count",
    batch_size=50_000,
)
pg.close()
```

完整双向示例见 `examples/12_sqlite_postgresql_roundtrip.py`。

## PostgreSQL 引擎

iosql 支持 PostgreSQL 作为后端引擎：

```python
db = Database("postgresql://user:pass@localhost:5432/mydb")
table = db.table(
    "ticks",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "bigint", "price": "double precision"},
    partition=Partition.duration("ts", every="1day"),
)
# query/latest/asof 在 PG14+ 使用数据库端 SQL 与命名游标
```

PostgreSQL 14+ 支持契约建表、批量写入、`query/latest/asof`、原生 JSON
`explain_select`、命名游标流式读取、复合索引及双向迁移。`Database.table_names()` 返回逻辑表，不暴露 native
partition 子表。

PostgreSQL 与 SQLite 的行为差异：

- `delete(where)` 返回删除行数；`where=None` 告警并转发 `delete_all()`，falsy 非 None 抛 `BadArgument`，不会静默全表删除。
- 列类型只接受封闭白名单（`blob→BYTEA`、`datetime→TIMESTAMP`）；未知类型抛 `BadArgument`。已有表缺契约列或未分区但声明分区时抛 `SchemaConflict`。
- 字符串 `fields` 只接受 `*` 或列名列表；含 SQL 语法抛 `BadArgument`；`select()` / `iter_select*()` 拒绝未知 kwargs。
- 批量写入按整批行列并集补 `NULL`，并按参数上限拆页；`commit=False` 保持事务，失败整体回滚。
- `order_by` 的 NULL 排序与 SQLite 一致（ASC → NULLS FIRST / DESC → NULLS LAST）。
- `upsert_all(..., where=...)` 的 `where` 为仅关键字参数且已废弃（忽略并告警）。

`duration(...)`、`range(...)` 和常见 `calendar(...)` 都可迁移到 PostgreSQL
native range partition。calendar 目标列支持 `text` / `date` / `timestamp`；
text 日期建议统一使用 ISO 日期（如 `2026-01-05`）或 compact 日期（如
`20260105`），不要在同一列混用格式。不确定时先在测试库用 `POSTGRES_DSN`
跑 `examples/11_sqlite_to_postgresql_migration.py` 的流程。

## 低层 mysqlite

低层模块适合独立 SQLite 文件工具场景，不是时序主入口：

```python
from iosql.utils.mysqlite import Database

db = Database("data.sqlite3")
db["items"].insert(
    {"id": 1, "name": "alpha"},
    pk=["id"],
    replace=True,
    ensure_table=True,
)
rows = db["items"].select("*", where=None, to_dict=True)
db.close()
```

使用 mysqlite 时注意：

- 需要动态建表时传 `ensure_table=True`。
- 需要动态加列时传 `ensure_column=True`。
- 批量写入优先 `insert_all()`；行中缺列会省略该列，由 SQLite 求值列默认值（含 `CURRENT_TIMESTAMP` 等表达式）。
- `insert()`/`executemany()` 传 `commit=False` 会被拒绝；`auto_commit=False` 时写入必须放在 `with db.transaction():` 中。
- `Database(":memory:")` 不被连接池支持，内存库请直接用 `sqlite3.connect(":memory:")`。
- 多线程或长期进程关注 `get_connection_pool_status()`、
  `get_process_connection_budget_status()`、`is_connection_pool_healthy()` 和
  `close()`。
- 事务用 `with db.transaction():` 上下文管理器。
- 通过外部连接执行 DDL 后，调用 `table.invalidate_cache()` 让列/默认值/主键缓存失效。

## 测试建议

- 用 `tmp_path` 构造 `sqlite://{tmp_path}/name.iosql`。
- 写入后同时断言 `WriteResult`、查询结果和 `table_parts()` 或 `segments()`。
- 涉及裁剪时断言 `query(...).explain().scanned_partitions` 和必要的 `scanned_segments`。
- 涉及 schema 契约时断言 `TableSpecConflict`、`SchemaConflict` 或 `BadArgument`。
- 涉及 close/reopen 时重新创建 `Database` 或复用已关闭的 `Database` 获取新表对象。

```python
import tempfile

def test_my_case():
    tmp_path = tempfile.mkdtemp()
    db = Database(f"sqlite://{tmp_path}/test.iosql")
    try:
        table = db.table("test", pk=["id"], columns={"id": "integer", "val": "float"})
        result = table.write({"id": 1, "val": 1.5})
        assert result.rows_seen == 1
        assert result.rows_written == 1
        rows = table.select("*", where=None, to_dict=True)
        assert len(rows) == 1
    finally:
        db.close()
```

## PostgreSQL 14+ 查询与诊断补充（PLAN-078）

- `query/latest/asof` 在数据库内完成筛选和排序；start/end/as_of 均包含边界。latest 按请求 symbol 顺序输出，同时间按剩余主键升序决胜。`latest(symbols=[])` 为空，`query(symbols=[])` 保留既有无筛选行为。
- 查询对象支持 `.to_list()` / `.one()` / `.iter_rows(batch_size=..., timeout_ms=...)` / `.iter_batches(...)` / `.explain(analyze=False, raw_plan=False)`。流式对象提前关闭会释放命名游标，显式写事务不会被读取擅自提交。同一 Table 的连接不应并发消费多个流。
- PG 的 `explain_select` 追加仅关键字 analyze/raw_plan；默认不执行、隐藏谓词常量，analyze=True 提供实际耗时与 buffer，raw_plan=True 可能含业务常量。QueryPlan 追加 engine/engine_plan/planning_time_ms/execution_time_ms/index_scans；PG 分区列表使用关系名，segment 列表为空。默认 scanned_partitions 为计划关系，ANALYZE 时只记录 Actual Loops>0 的关系。
- `create_index(column=None, order='asc', exist_ok=True, *, columns=None)` 支持复合 B-tree；与主键等价时复用。`drop_index(column=None, *, columns=None)` 仅删除当前表带 iosql 标记且定义匹配的二级索引；不能按名称误删用户索引或约束索引。
- 重开物理表验证声明列类型、主键顺序、分区形态和 RANGE 单列键；分区主键必须包含分区列。
- upsert_all 按输入主键增量合并，缺字段保留旧值，显式 None 写 NULL，重复主键按输入顺序合并。count_rows 是精确计数，可能全表扫描。
- 完整示例：`examples/15_postgresql_timeseries_query.py`。百万/千万行基准：仓库 `benchmarks/benchmark_postgresql_query.py`。

指定 symbols 的单 symbol 分组 latest 会在目录确认有效 symbol/time B-tree 前缀索引后使用 LATERAL LIMIT 1；无索引/多维分组/无 symbols 时使用 DISTINCT ON。

迁移时可统一调整目标分区：

```python
result = ticks.migrate_to(
    "parquet://db/hourly.iosql",
    specs={"ticks": {"partition": "duration(ts,every=1hour)"}},
    verify="row-count",
)
```

多表同样传 `specs`，键使用源名。迁移全程禁止源/目标协作读写，本地源默认在源库内暂存，PostgreSQL 源使用系统临时目录；全部转换后统一发布。`temp_dir` 与 `lock_timeout` 可选，具体锁、恢复、兼容和结果语义见 design-contract.md 的 PLAN-083 一节。
