# iosql

`iosql` 是一个接近原生 SQLite 的自动分区存储库。它把同一张逻辑表拆到多个 SQLite 分区文件中，保留 SQLite 的简单部署方式，同时提供逻辑分区、物理 row segment、查询裁剪、批量写入和布局诊断能力。时间序列只是普通分区表的一种 helper 用法。

## 特性

- **普通 Table 主入口**：用 `Database.table(..., pk=..., partition=..., segment=...)` 声明主键、列类型、逻辑分区和物理分段。
- **任意可排序字段分区**：`Partition.range(...)`、`Partition.duration(...)`、`Partition.calendar(...)` 都是普通分区策略，不要求核心 API 存在 `time` 字段。
- **物理 row segment**：同一 logical partition 可按行数自动拆成多个 SQLite 文件，也可以不配置 logical partition，仅用 `__all__` + row segment 控制文件大小。
- **二级查询裁剪**：`select(where=...)` 先裁剪 logical partitions，再通过 segment `stats_json` 跳过无关物理文件。
- **批量写入**：`insert_all()` 和 `write_buffer()` 可直接使用表契约写入。
- **布局维护**：`layout_report()` 检查分区行数，`repartition_to()` 可生成新的分区布局。
- **时序 helper**：`Database.timeseries()` 保留为快捷封装，适合 `query(start/end)`、`latest()`、`asof()` 等业务查询。

## Partition 与 Segment 的关系

`partition`（逻辑分区）和 `segment`（物理分段）是 iosql 核心的二级文件管理机制，两者独立配置、组合使用。

```
Partition（逻辑分区）          Segment（物理分段）
按业务键做第一级路由         按行数做第二级文件滚动
e.g. ts=1735689600000         每个逻辑分区下可拆分多个 .sqlite3 文件
```

### 四种组合

| Partition | Segment | 数据路由 | 文件滚动 | 适用场景 |
|:---------:|:-------:|---------|---------|---------|
| ✅ | ✅ | 按分区键路由 | 按行数自动创建新文件 | 高频时序数据（tick/K线），单分区数据量大 |
| ✅ | ❌ | 按分区键路由 | 不滚动，单文件 | 低频数据（日线/财报），单分区数据量可控 |
| ❌ | ✅ | 全部到 `__all__` | 按行数自动创建新文件 | 日志/事件流，只需控制文件大小 |
| ❌ | ❌ | 无路由 | 不滚动 | 简单 KV 表、元数据、配置表 |

### 目录结构

**完整模式（partition + segment）**：
```text
ticks/
└── data/
    ├── 1735689600000.sqlite3        # partition=0000, segment=0
    ├── 1735689600000-1.sqlite3      # 同一逻辑分区, segment=1（行数满了）
    └── 1735776000000.sqlite3        # partition=0000, segment=0
```

**纯 segment（无 partition）**：
```text
events/
└── data/
    ├── __all__-0.sqlite3            # segment=0
    ├── __all__-1.sqlite3            # segment=1
    └── __all__-2.sqlite3            # segment=2
```

**纯 partition（无 segment）**：
```text
daily_bars/
└── data/
    ├── 1735689600000.sqlite3        # 单文件（会无限增长）
    └── 1735776000000.sqlite3
```

### 查询裁剪

配置 `segment` 后，查询时执行**二级裁剪**：

1. **一级（logical partition）**：按分区键跳过不相关的时间段/数值范围
2. **二级（physical segment）**：通过 segment `stats`（每列 min/max）和 `session_min/session_max` 跳过无关文件

`explain_select()` 返回的 `QueryPlan` 同时展示两级裁剪结果。

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

下面是核心 `Table` API。除 `partition=...` 外，它尽量保持 SQLite 表的心智：声明表、写入行、用 `where` 查询。

```python
from iosql import Database, Partition

db = Database("sqlite://database/market.iosql")

ticks = db.table(
    "ticks",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition=Partition.duration("ts", every="1day"),
)

result = ticks.insert_all([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
    {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
])

where = [
    {"field": "symbol", "op": "==", "value": "AAPL"},
    {"field": "ts", "op": "between_and", "value": [1735689600000, 1735776000000]},
]
rows = ticks.select(
    "*",
    where=where,
    order_by="ts",
    to_dict=True,
)

plan = ticks.explain_select(where=where, order_by="ts")

print(result.rows_seen)
print(rows)
print(plan.scanned_partitions)
db.close()
```

## 常用 API

### 创建或打开表

```python
from iosql import Partition, Segment

table = db.table(
    "ticks",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition="duration(ts,every=1hour)",
    segment=Segment.rows(max_rows=500_000),
    order_key="ts",
)
```

`partition` 描述按哪个字段做逻辑路由；`segment` 描述单个 logical partition 下物理 SQLite 文件如何按行数滚动。字段可以是时间戳、序号、offset、版本号等任意可排序字段。

`partition` 可以使用对象 API，也可以使用统一字符串表达。字符串表达更适合 CLI、文档和诊断输出：

```python
db.table("ticks", pk=["symbol", "ts"], partition="duration(ts,every=1day)")
db.table("events", pk=["event_date", "name"], partition="calendar(event_date,every=1day,tz=Asia/Shanghai)")
db.table("daily_bars", pk=["symbol", "date"], partition="calendar(date,every=4day,tz=Asia/Shanghai)")
db.table("items", pk=["id"], partition="range(id,width=100000)")
```

时序数据优先使用 `duration(...)` 或 `calendar(...)`。`range(...)` 主要用于 id、offset、版本号等非时间数值字段；历史库中基于 `partition_key` 的 `range(...)` 可以作为兼容迁移方案。

等价对象 API：

```python
Partition.duration("ts", every="1day")
Partition.calendar("event_date", every="1day", timezone="Asia/Shanghai")
Partition.range("id", every=100000)
```

如果偏好时序业务 helper，可以使用 `timeseries()`。它内部等价于 `Database.table(..., partition=Partition.duration(...), order_key=time)`：

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
table.insert_all({"symbol": "AAPL", "ts": 1735689600000, "price": 187.4})

with table.write_buffer(max_rows=10_000) as buffer:
    buffer.append([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
    ])
```

### 大批量写入性能

历史回灌和高频流不要逐行调用 `write()`。日线数据建议从每批 `50_000` 行开始测；orderbook/tick 建议从每批 `50_000` 至 `100_000` 行开始测。

20 年、每天约 5000 支股票的日线约为 2500 万行。此类数据通常按年分区比按日分区更合理：

```python
daily = db.table(
    "daily",
    pk=["symbol", "date"],
    columns={
        "symbol": "text",
        "date": "text",
        "open": "real",
        "high": "real",
        "low": "real",
        "close": "real",
        "volume": "integer",
        "amount": "real",
    },
    partition="calendar(date,every=1year,tz=Asia/Shanghai)",
    order_key="date",
    conflict="append",
)

for rows in source_batches:
    daily.write(rows)
```

orderbook/tick 优先使用 append-only + 日逻辑分区 + row segment：

```python
from iosql import Segment

book = db.table(
    "orderbook",
    pk=["event_id"],
    columns={
        "event_id": "integer",
        "symbol": "text",
        "ts": "integer",
        "sequence": "integer",
        "side": "text",
        "price": "real",
        "size": "integer",
    },
    partition="duration(ts,every=1day)",
    segment=Segment.rows(
        max_rows=1_000_000,
        stats_columns=["ts", "sequence"],
    ),
    order_key="ts",
    conflict="append",
)

with book.write_buffer(max_rows=100_000) as buffer:
    for rows in stream_batches:
        buffer.append(rows)
```

默认 SQLite 配置是适合持续导入的 balanced 模式：`journal_mode=WAL`、`synchronous=NORMAL`、64 MiB page cache。需要更强断电保障时显式改为 `FULL`：

```python
db = Database(
    "sqlite:///media/lsl/G1/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
    pragma_dict={
        "journal_mode": "wal",
        "synchronous": "full",
        "cache_size": "-64000",
        "temp_store": "memory",
        "busy_timeout": "30000",
    },
)
```

`max_open_partitions` 是每张逻辑表允许缓存的分区数据库数量，
`max_connections_per_file` 是每个 SQLite 文件的连接池上限。默认值分别为 `5`
和 `1`，由 iosql 在进程内强制执行，不依赖提高 `ulimit -n`。WAL 模式下每条
SQLite 连接可能同时占用数据库、WAL 和 SHM 文件描述符；包含两个元数据库时，
单表稳态 FD 预算可粗略按
`(max_open_partitions + 2) * max_connections_per_file * 3` 估算。

大量分区扫描使用 LRU 淘汰旧分区连接。`Database.close()` 或上下文管理器仍是
确定性释放资源的首选方式；低层清理注册表使用弱引用，遗弃对象不会再被全局注册表
永久保活，但不应以垃圾回收替代正常的生命周期管理。

只有原始抓取数据可确定重放时才考虑 `synchronous=OFF`；掉电或系统崩溃时它可能损坏最近事务。

在 Linux ext4 HDD 上直接运行目标盘 benchmark：

```bash
python benchmarks/benchmark_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --rows 25000000 \
  --batch-size 50000 \
  --profile balanced

python benchmarks/benchmark_write.py orderbook \
  --root /media/lsl/G1/iosql-bench \
  --rows 100000000 \
  --batch-size 100000 \
  --segment-rows 1000000 \
  --profile balanced

python benchmarks/stress_open_files.py \
  --root /media/lsl/G1/iosql-fd-stress \
  --partitions 500 \
  --abandoned-instances 150 \
  --max-open-partitions 5 \
  --max-connections-per-file 1
```

先用较小样本自动比较 G1/ext4 与 Z1/NTFS，并扫描批次和 durability：

```bash
python benchmarks/sweep_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --root /media/lsl/Z1/iosql-bench \
  --rows 2000000 \
  --batch-sizes 20000,50000,100000 \
  --profiles balanced,durable \
  --output /tmp/iosql-daily-disk-compare.json
```

扫描默认删除每个组合的临时数据库，只保留汇总 JSON 和同名 Markdown；确认最佳参数后，再在 G1 上运行 2500 万行完整测试。

比较 ext4 与 NTFS 时必须保持 workload、行数、batch size 和 profile 相同。Linux 本地 ext4 通常更适合 SQLite WAL，但最终以目标机器实测为准。HDD 单盘建议每张逻辑表保持单 writer，避免多个导入任务并行制造随机寻道。

`Segment.rows()` 默认统计所有列的 min/max，以支持任意字段裁剪。高频宽表如果只按时间、序列等字段查询，可通过 `stats_columns` 限制可选统计列；partition column、time、`order_key` 和 `session_column` 会自动保留。未统计字段仍可查询，只是不能依靠 segment stats 跳过文件。

### 查询

```python
where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between_and", "value": [1735689600000, 1735776000000]},
    {"field": "venue", "op": "==", "value": "NASDAQ"},
]
rows = table.select(
    "*",
    where=where,
    order_by="ts desc",
    limit=100,
    to_dict=True,
)

plan = table.explain_select(where=where, order_by="ts desc", limit=100)
segments = table.segments()
```

`where` 支持 dict/list/`Case`，也支持受限文本表达式。文本表达式会被解析为内部条件，不会作为原始 SQL 拼接；当前支持比较、`and` / `or`、括号、`between` 和 `in`。

`segments()` 返回物理文件级 manifest，包含 logical partition、segment id、文件 key、行数和列级 `stats`。`explain_select()` 同时返回 logical partitions 和 physical segments 的候选/扫描/跳过信息。

`timeseries()` helper 额外提供业务查询：

```python
rows = ticks.query(start=1735689600000, end=1735776000000).to_list()

snapshot = table.latest(
    symbols=["AAPL", "MSFT"],
    as_of=1735776000000,
    by=["symbol", "timeframe"],
    columns=["symbol", "timeframe", "ts", "price"],
).to_list()
```

开启物理 segment 后，append-only 表不主动跨 sealed segments 查重。需要跨 segment upsert 时，后续应引入全局主键索引和 tombstone。

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

如果数据使用日期字符串，可以直接使用 `Partition.calendar(...)`；对应 `timeseries()` 快捷版本见 `examples/02_calendar_events.py`：

```python
events = db.table(
    "events",
    pk=["event_date", "name"],
    columns={"event_date": "text", "name": "text", "city": "text"},
    partition=Partition.calendar("event_date", every="1day", timezone="Asia/Shanghai"),
)

events.insert_all({"event_date": "2026-01-06", "name": "review", "city": "Beijing"})
rows = events.select(
    "*",
    where={"field": "event_date", "op": "==", "value": "2026-01-06"},
    to_dict=True,
)
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

new_spec = table.spec().with_partition(partition="duration(ts,every=1hour)")
result = table.repartition_to("sqlite://database/market-hourly.iosql", spec=new_spec)
print(result.rows_copied, result.partitions_created)
```

CLI 重分区推荐使用 `--partition`：

```bash
iosql repartition -s sqlite://database/market.iosql -t ticks \
  --partition 'duration(ts,every=1hour)' \
  --target sqlite://database/market-hourly.iosql
```

`iosql doctor` 会按布局行数给出多条候选命令。对带 `time` 契约或明显时间列的表，会优先推荐时间分区，例如：

```text
suggestions:
  1. 推荐: 按时间列 date 使用 4day (max/avg≈4, 目标<=500,000), 估算~465,136 行/分区
     → iosql repartition --source DB --table daily_bars --partition 'calendar(date,every=4day,tz=Asia/Shanghai)' --replace-source --yes
  2. 兼容: 保留当前数值分区键 partition_key，width=4 (扩大 4 倍), 估算~465,136 行/分区
     → iosql repartition --source DB --table daily_bars --partition 'range(partition_key,width=4)' --replace-source --yes
```

需要直接替换源库时必须显式声明风险：

```bash
iosql repartition -s sqlite://database/market.iosql -t ticks \
  --partition 'duration(ts,every=1hour)' \
  --replace-source --yes
```

## 示例

示例位于 `examples/`，按学习顺序编号：

| 文件 | 场景 |
| --- | --- |
| `01_quickstart_table.py` | 普通 `Table` + `Partition.duration(...)` 写入、查询和 explain。 |
| `01_quickstart_timeseries.py` | `Database.timeseries()` helper 版写入和查询。 |
| `02_calendar_events_table.py` | 普通 `Table` + `Partition.calendar(...)` 处理日期字符串分区。 |
| `02_calendar_events.py` | `Database.timeseries()` 快捷版处理日期字符串和 calendar 分区。 |
| `03_buffered_ingest_and_query_plan.py` | 批量缓冲写入、row segment 和查询计划。 |
| `04_layout_report_and_repartition.py` | 布局诊断和重分区。 |
| `05_partition_segments_smoke.py` | 逻辑分区 + 物理 segment 拆分和二级查询计划。 |
| `06_row_segment_late_data.py` | 纯 row segment、晚到数据和 stats pruning。 |
| `07_partition_string_repartition_cli.py` | 字符串 partition 和 CLI `repartition --partition`。 |
| `08_doctor_time_first_suggestions.py` | `doctor` 对历史 `partition_key` 表优先推荐时间分区。 |

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
python examples/03_buffered_ingest_and_query_plan.py
python examples/04_layout_report_and_repartition.py
python examples/05_partition_segments_smoke.py
python examples/06_row_segment_late_data.py
python examples/07_partition_string_repartition_cli.py
python examples/08_doctor_time_first_suggestions.py
PYTHONPATH=. python examples/finance/01_kline_bars.py
```

低层 SQLite 工具示例位于 `examples/mysqlite/`，用于 `iosql.utils.mysqlite` 的 schema-on-write、批量写入、冲突处理和连接池清理。它们不是新的主 API 入口。

## 存储结构

示例中的 `sqlite://database/market.iosql` 会生成一个目录式数据库。开启 segment 后，同一逻辑分区可以拆分多个物理文件：

```text
database/
└── market.iosql/
    └── ticks/
        ├── meta.sqlite3            # 表元数据、分区信息、segment 清单
        └── data/
            ├── 1735689600000.sqlite3     # partition=1735689600000, segment=0
            ├── 1735689600000-1.sqlite3   # 同一分区, segment=1（行数>max_rows 时创建）
            └── 1735776000000.sqlite3     # partition=1735776000000, segment=0
```

不配置 `partition` 时，所有数据使用 `__all__` 作为逻辑分区名。不配置 `segment` 时，每个逻辑分区仅一个文件。

## 注意事项

- 核心 `Database.table(...)` 不需要 `time` 字段；分区字段由 `Partition.*(...)` 指定。
- `timeseries()` 仍保留为 helper，适合需要 `query(start/end/date)`、`latest()`、`asof()` 的业务场景。
- 多分区查询支持全局 `order_by` 和 `limit`，但结果会在 Python 层合并排序，扫描范围应尽量通过时间条件收窄。
- 配置 `Segment.rows(...)` 后，同一 logical partition 可以拆成多个物理 SQLite 文件；晚到数据会进入新的 active/delta segment。
- `layout_report()` 只做诊断，不会修改原库；重分区请使用 `repartition_to()` 写入新的目标库。
- `columns={"col": "type"}` 中声明的列类型在自动建表时优先生效；未声明的列由数据推断。
- PostgreSQL URI 格式为 `postgresql://user:pass@host:port/dbname`，与 SQLite 引擎共享同一套分区和查询 API。
- `Segment.rows` 表当前不支持 `upsert`/`replace` 覆盖写；需要覆盖写时请关闭 segment 或使用 `append`/`ignore`。

## 变更记录

- v0.2.5 - 修复列类型推断 Bug（用户声明的 `columns` 类型此前被静默忽略）；修复 PostgreSQL URI 解析（此前不可达）；修复元数据查询 SQL 注入（5 处 f-string 改为参数化）；修复 `Partition | None` 在 Python 3.9 上的兼容性；修复 `create_table` 静默空操作；清理重复异常定义和死代码文件。
- v0.2.4 - 修复查询语义，重组 core/query 与 engines 包结构，删除旧 query/sqlite/postgresql 兼容入口。
- v0.2.1 - 引入时序表契约、布局诊断、重分区规划和 pyproject 打包入口。
- v0.2.0 - 新分区系统，支持 `DurationPartition` 和 `CalendarPartition`。
- v0.1.2 - 添加 `create_index`。
- v0.1.1 - 添加分区格式。
- v0.1.0 - 初始版本。
