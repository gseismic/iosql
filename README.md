# iosql

`iosql` 是一个面向时序数据的 SQLite 分区存储库。它把同一张逻辑表拆到多个 SQLite 分区文件中，保留 SQLite 的简单部署方式，同时提供时序表契约、分区裁剪、批量写入、布局诊断和重分区能力。

## 特性

- **时序表契约**：用 `Database.timeseries()` 或 `TableSpec` 明确主键、时间列、列类型和分区策略。
- **分区存储**：按时间把数据写入多个 SQLite 文件，降低单文件膨胀风险。
- **分区裁剪**：按 `start` / `end` / `date` 查询时自动跳过不相关分区。
- **批量写入**：`write()` 和 `write_buffer()` 提供面向时序表的写入入口。
- **布局维护**：`layout_report()` 检查分区行数，`repartition_to()` 可生成新的分区布局。
- **日历分区**：支持带时区的日、月、年等日历分区。

## 安装

要求 Python 3.9 或更高版本。

开发安装：

```bash
python -m pip install -e .
```

构建 wheel：

```bash
python -m pip wheel . --no-deps -w dist
```

项目使用 `pyproject.toml` 作为打包入口，版本号来自 `iosql.version.__version__`。

## 快速开始

```python
from iosql import Database

db = Database("sqlite://database/market.iosql")

ticks = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1day",
)

result = ticks.write([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
    {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
])

rows = ticks.query(
    symbols="AAPL",
    start=1735689600000,
    end=1735776000000,
    order_by="ts",
).to_list()

plan = ticks.query(symbols="AAPL", start=1735689600000).explain()

print(result.rows_seen)
print(rows)
print(plan.scanned_partitions)
db.close()
```

## 常用 API

### 创建或打开时序表

```python
table = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1hour",
    timezone="UTC",
)
```

### 写入

```python
table.write({"symbol": "AAPL", "ts": 1735689600000, "price": 187.4})

with table.write_buffer(max_rows=10_000) as buffer:
    buffer.append([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
    ])
```

### 查询

```python
rows = table.query(
    symbols=["AAPL", "MSFT"],
    start=1735689600000,
    end=1735776000000,
    order_by="ts desc",
    limit=100,
).to_list()

plan = table.query(start=1735689600000, end=1735776000000).explain()
```

### 日历分区

`Database.timeseries()` 默认适合毫秒时间戳。如果数据使用日期字符串，可以直接声明 `TableSpec`：

```python
from iosql import Database, PartitionSpec, TableSpec, TimeColumn

spec = TableSpec(
    primary_key=["event_date", "name"],
    time=TimeColumn("event_date", unit="date", timezone="Asia/Shanghai"),
    partition=PartitionSpec.by_time("event_date", every="1day", kind="calendar"),
    columns={"event_date": "text", "name": "text", "city": "text"},
)

events = Database("sqlite://database/events.iosql").table("events", spec=spec)
events.write({"event_date": "2026-01-06", "name": "review", "city": "Beijing"})
rows = events.query(date="2026-01-06").to_list()
```

### 布局诊断和重分区

```python
report = table.layout_report()
print(report.total_rows, report.recommendation)

new_spec = table.spec().with_partition(every="1hour")
result = table.repartition_to("sqlite://database/market-hourly.iosql", spec=new_spec)
print(result.rows_copied, result.partitions_created)
```

## 示例

示例位于 `examples/`，按学习顺序编号：

| 文件 | 场景 |
| --- | --- |
| `01_quickstart_timeseries.py` | 入门时序表写入和查询。 |
| `02_calendar_events.py` | 日期字符串和日历分区。 |
| `03_buffered_ingest_and_query_plan.py` | 批量缓冲写入和查询计划。 |
| `04_layout_report_and_repartition.py` | 布局诊断和重分区。 |

运行：

```bash
python examples/01_quickstart_timeseries.py
```

## 存储结构

示例中的 `sqlite://database/market.iosql` 会生成一个目录式数据库：

```text
database/
└── market.iosql/
    └── ticks/
        ├── meta.sqlite3
        └── data/
            ├── 1735689600000.sqlite3
            └── 1735776000000.sqlite3
```

`meta.sqlite3` 保存表元数据和分区信息；每个分区文件保存该时间范围内的真实数据。

## 注意事项

- `Database.timeseries()` 的时间列默认使用毫秒时间戳。
- `Table.write()` 需要表存在 `TableSpec`，推荐优先使用 `Database.timeseries()` 创建时序表。
- 多分区查询支持全局 `order_by` 和 `limit`，但结果会在 Python 层合并排序，扫描范围应尽量通过时间条件收窄。
- `layout_report()` 只做诊断，不会修改原库；重分区请使用 `repartition_to()` 写入新的目标库。

## 变更记录

- v0.2.1 - 引入时序表契约、布局诊断、重分区规划和 pyproject 打包入口。
- v0.2.0 - 新分区系统，支持 `DurationPartition` 和 `CalendarPartition`。
- v0.1.2 - 添加 `create_index`。
- v0.1.1 - 添加分区格式。
- v0.1.0 - 初始版本。
