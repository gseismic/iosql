# iosql

`iosql` 是一个面向时序数据的 SQLite 分区存储库。它把同一张逻辑表拆到多个 SQLite 分区文件中，保留 SQLite 的简单部署方式，同时提供时序表契约、分区裁剪、批量写入、布局诊断和重分区能力。

## 特性

- **双入口表契约**：可用 `Database.table(..., spec=TableSpec(...))` 显式声明主键、时间列、列类型和分区策略，也可用 `Database.timeseries()` 快速创建同等契约。
- **分区存储**：按时间把数据写入多个 SQLite 文件，降低单文件膨胀风险。
- **物理 segment**：同一逻辑时间分区可按行数自动拆成多个 SQLite 文件，适合高频金融数据。
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

下面是原始 `Table` + `TableSpec` 显式版本，对应快捷版见 `examples/01_quickstart_timeseries.py`。

```python
from iosql import Database, PartitionSpec, TableSpec, TimeColumn

db = Database("sqlite://database/market.iosql")

spec = TableSpec(
    primary_key=["symbol", "ts"],
    time=TimeColumn("ts", unit="ms", timezone="UTC"),
    partition=PartitionSpec.by_time("ts", every="1day"),
    columns={"symbol": "text", "ts": "integer", "price": "float"},
)
ticks = db.table("ticks", spec=spec)

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

### 创建或打开表

```python
from iosql import PartitionSpec, TableSpec, TimeColumn

spec = TableSpec(
    primary_key=["symbol", "ts"],
    time=TimeColumn("ts", unit="ms", timezone="UTC"),
    partition=PartitionSpec.by_time("ts", every="1hour"),
    columns={"symbol": "text", "ts": "integer", "price": "float"},
)
table = db.table("ticks", spec=spec)
```

`time` 描述业务时间列，用于 `start/end/date/latest/asof` 的时间解释；`partition` 描述物理分区布局，用于写入落盘和查询裁剪。两者通常指向同一列，但职责不同。

如果偏好更短的写法，可以使用 `timeseries()`。它内部会创建同等的 `TableSpec`：

```python
table = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1hour",
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
    where="venue = 'NASDAQ'",
    order_by="ts desc",
    limit=100,
).to_list()

plan = table.query(start=1735689600000, end=1735776000000).explain()
segments = table.segments()

snapshot = table.latest(
    symbols=["AAPL", "MSFT"],
    as_of=1735776000000,
    by=["symbol", "timeframe"],
    columns=["symbol", "timeframe", "ts", "price"],
).to_list()
```

`where` 支持 dict/`Case`，也支持受限文本表达式。文本表达式会被解析为内部条件，不会作为原始 SQL 拼接；当前支持比较、`and` / `or`、括号、`between` 和 `in`。

`segments()` 返回物理文件级 manifest，包含逻辑分区、segment id、文件 key、行数、时间范围和 session 范围。`query().explain()` 同时返回逻辑分区和物理 segment 的候选/扫描/跳过信息。

开启物理 segment 后，`write(mode="upsert")` / `write(mode="replace")` / `write(mode="ignore")` 仍按逻辑表主键处理已有行，不会因为同一逻辑分区拆成多个文件而绕过主键语义。

`latest()` / `asof()` 用于金融截面查询，默认返回每个 symbol 截至 `as_of` 的最新一行；多 symbol 查询会按传入的 symbol 顺序返回。K 线、orderbook 等多维数据可传 `by=["symbol", "timeframe"]` 或 `by=["symbol", "venue"]`，`group_by` 是 `by` 的等价别名。

### 交易日 / Session

`date=` 按时间列和时区解释为自然日。期货夜盘、跨时区行情等场景应显式声明交易日/session 列：

```python
ticks = db.timeseries(
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

rows = ticks.query(session="2026-01-06", symbols="AU").to_list()
snapshot = ticks.latest(
    session="2026-01-06",
    symbols="AU",
    as_of=1767661200000,
).one()
```

`session=` 会编译成对 `session_column` 的等值或 `in` 条件，可与 `symbols`、`start/end`、`date`、`where`、`latest()` 和 `asof()` 组合。新写入的 segment 会记录 `session_min/session_max`，`explain()` 可显示 `pruned_by_segment_session_range`。

### 日历分区

如果数据使用日期字符串，可以在原始 `TableSpec` 中声明 `TimeColumn(unit="date")` 和 calendar 分区；对应 `timeseries()` 快捷版本见 `examples/02_calendar_events.py`：

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

### 金融数据建议

| 数据类型 | 时间字段 | 推荐分区 | 主键示例 |
| --- | --- | --- | --- |
| K 线 | `ts` 毫秒时间戳 | `1day` duration | `symbol, timeframe, ts` |
| orderbook | `ts` 毫秒时间戳 | `1hour` duration | `symbol, venue, ts, side, level` |
| 期货夜盘 tick | `ts` 毫秒时间戳 + `trading_date` | `1day` duration + `session_column` | `symbol, ts` |
| 财报 | `published_date` 日期字符串 | `1month` calendar | `symbol, fiscal_period, statement` |
| 新闻/公告 | `event_ts` 毫秒时间戳或 `published_date` 日期 | duration 或 calendar | `symbol, event_ts, event_id` |

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
| `01_quickstart_table.py` | 原始 `Table` + `TableSpec` 写入、查询和 duration 分区。 |
| `01_quickstart_timeseries.py` | `Database.timeseries()` 快捷版写入和查询。 |
| `02_calendar_events_table.py` | 原始 `Table` + `TableSpec` 处理日期字符串和 calendar 分区。 |
| `02_calendar_events.py` | `Database.timeseries()` 快捷版处理日期字符串和 calendar 分区。 |
| `03_buffered_ingest_and_query_plan.py` | 批量缓冲写入和查询计划。 |
| `04_layout_report_and_repartition.py` | 布局诊断和重分区。 |
| `05_partition_segments_smoke.py` | 多分区写入、物理 segment 拆分和查询计划。 |

金融场景示例位于 `examples/finance/`：

| 文件 | 场景 |
| --- | --- |
| `01_kline_bars.py` | K 线 OHLCV 写入和时间窗口查询。 |
| `02_orderbook_snapshots.py` | orderbook 快照批量写入和复合主键。 |
| `03_financial_reports.py` | 财报日期字段、月度日历分区和日期范围查询。 |
| `04_news_announcements.py` | 新闻/公告事件按时间和类型过滤。 |

运行：

```bash
python examples/01_quickstart_table.py
python examples/01_quickstart_timeseries.py
python examples/02_calendar_events_table.py
python examples/02_calendar_events.py
python examples/05_partition_segments_smoke.py
PYTHONPATH=. python examples/finance/01_kline_bars.py
```

低层 SQLite 工具示例位于 `examples/mysqlite/`，用于 `iosql.utils.mysqlite` 的 schema-on-write、批量写入、冲突处理和连接池清理。它们不是新的主 API 入口。

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

- `TableSpec` 中的 `TimeColumn(unit="ms")` 适合毫秒时间戳，也是 `Database.timeseries()` 的默认行为。
- `Table.write()` 需要表存在 `TableSpec`；可以显式传给 `Database.table(..., spec=...)`，也可以通过 `Database.timeseries()` 自动生成。
- 多分区查询支持全局 `order_by` 和 `limit`，但结果会在 Python 层合并排序，扫描范围应尽量通过时间条件收窄。
- 新建时序表会按 `max_rows_per_file` 自动拆分物理 segment；旧的单分区文件仍可读取，不会被原地迁移。
- `layout_report()` 只做诊断，不会修改原库；重分区请使用 `repartition_to()` 写入新的目标库。

## 变更记录

- v0.2.1 - 引入时序表契约、布局诊断、重分区规划和 pyproject 打包入口。
- v0.2.0 - 新分区系统，支持 `DurationPartition` 和 `CalendarPartition`。
- v0.1.2 - 添加 `create_index`。
- v0.1.1 - 添加分区格式。
- v0.1.0 - 初始版本。
