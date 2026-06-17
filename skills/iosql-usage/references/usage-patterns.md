# iosql 使用模式

## 毫秒时序表：原始 Table 版

```python
from iosql import Database, Partition, Segment, TableSpec, TimeColumn

db = Database(f"sqlite://{db_path}")
spec = TableSpec(
    primary_key=["symbol", "ts"],
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
    {"field": "ts", "op": "between_and", "value": [1735689600000, 1735776000000]},
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
    primary_key=["symbol", "ts"],
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
    primary_key=["event_date", "name"],
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
    where={"field": "seq", "op": "between_and", "value": [0, 20000]},
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
    where={"field": "seq", "op": "between_and", "value": [0, 3]},
)
print("扫描:", plan.scanned_segments)
print("跳过:", plan.segment_skip_reasons)
db.close()
```

## 缓冲批量写入（write_buffer）

适合大批量数据摄入，自动分批 flush：

```python
base_ts = 1735689600000
with ticks.write_buffer(max_rows=4) as buf:
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
    primary_key=["symbol", "ts"],
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
    primary_key=["symbol", "timeframe", "ts"],
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

`by` 支持多列分组，`group_by` 是等价别名。不传 `by` 时默认按 `symbol_column`（默认 `"symbol"`）分组。

## 写入模式

- `mode="upsert"`：默认模式，同主键覆盖旧行。
- `mode="replace"`：显式 replace，同主键覆盖旧行。
- `mode="ignore"`：同主键已有行时跳过。
- `mode="append"`：严格追加，重复主键应抛出数据库约束错误。

```python
result = table.write(data, mode="upsert")
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
# 条件删除
deleted = ticks.delete(where={"field": "symbol", "op": "==", "value": "DELISTED"})
print(f"删除了 {deleted} 行")

# 清空表（需显式确认）
ticks.truncate(confirm=True)
```

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
        {"field": "ts", "op": "between_and", "value": [start_ts, end_ts]},
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
    print(seg["part_key"], seg["row_count"], seg["sealed"])
```

## Where 条件构造

### Dict 格式（推荐）

```python
# 单条件
where = {"field": "symbol", "op": "==", "value": "AAPL"}

# 列表 → AND 组合
where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between_and", "value": [start, end]},
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
c2 = Case("ts", "between_and", [start, end])

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

# 重分区
new_spec = table.spec().with_partition(every="1hour")
result = table.repartition_to(
    f"sqlite://{target_path}",
    spec=new_spec,
)
```

`layout_report()` 只做诊断。`repartition_to()` 写新库，适合离线迁移和验证，不要假设它会原地修改源库。

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
# 后续 write/select 等 API 与 SQLite 一致
```

PostgreSQL 模式下的分区策略和查询行为与 SQLite 引擎保持一致（详情见 `iosql/postgresql/`）。

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
- 批量写入优先 `insert_all()`。
- 多线程或长期进程关注 `get_pool_status()`、`is_pool_healthy()` 和 `close()`。
- 事务用 `with db.transaction():` 上下文管理器。

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
