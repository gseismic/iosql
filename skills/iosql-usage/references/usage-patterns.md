# iosql 使用模式

## 毫秒时序表

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

## 日期字符串和日历分区

```python
events = db.timeseries(
    "events",
    time="event_date",
    time_unit="date",
    partition_kind="calendar",
    partition_every="1day",
    timezone="Asia/Shanghai",
    primary_key=["event_date", "name"],
    columns={"event_date": "text", "name": "text", "city": "text"},
)

events.write({"event_date": "2026-01-06", "name": "review", "city": "Beijing"})
rows = events.query(date="2026-01-06").to_list()
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

## 写入模式

- `mode="upsert"`：默认模式，同主键覆盖旧行。
- `mode="replace"`：显式 replace，同主键覆盖旧行。
- `mode="ignore"`：同主键已有行时跳过。
- `mode="append"`：严格追加，重复主键应抛出数据库约束错误。

## 布局和重分区

```python
report = table.layout_report()
segments = table.segments()

new_spec = table.spec().with_partition(every="1hour")
result = table.repartition_to(
    f"sqlite://{target_path}",
    spec=new_spec,
)
```

`layout_report()` 只做诊断。`repartition_to()` 写新库，适合离线迁移和验证，不要假设它会原地修改源库。

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

## 测试建议

- 用 `tmp_path` 构造 `sqlite://{tmp_path}/name.iosql`。
- 写入后同时断言 `WriteResult`、查询结果和 `table_parts()` 或 `segments()`。
- 涉及裁剪时断言 `query(...).explain().scanned_partitions` 和必要的 `scanned_segments`。
- 涉及 schema 契约时断言 `TableSpecConflict`、`SchemaConflict` 或 `BadArgument`。
- 涉及 close/reopen 时重新创建 `Database` 或复用已关闭的 `Database` 获取新表对象。
