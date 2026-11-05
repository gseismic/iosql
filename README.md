# iosql

`iosql` 是一个接近原生 SQLite 的自动分区存储库。它把同一张逻辑表拆到多个 SQLite 分区文件中，保留 SQLite 的简单部署方式，同时提供逻辑分区、物理 row segment、查询裁剪、批量写入和布局诊断能力。时间序列只是普通分区表的一种 helper 用法。

支持 Parquet 和可选的 PostgreSQL 后端；同一套表契约与读写接口可用于不同后端。

## 特性

- **普通 Table 主入口**：用 `Database.table(..., pk=..., partition=..., segment=..., time=...)` 声明主键、列类型、逻辑分区和物理分段；`time=` / `session_column=` 让契约感知查询直达普通分区表。
- **Parquet 后端**：`Database("parquet://...")` 把业务数据保存为 ZSTD 压缩的 Parquet 文件，使用 DuckDB 查询，支持分区、segment、流式排序和 SQLite 双向迁移。
- **任意可排序字段分区**：`Partition.range(...)`、`Partition.duration(...)`、`Partition.calendar(...)` 都是普通分区策略；duration/calendar 分区未声明 time 时会自动反推。
- **物理 row segment**：同一 logical partition 可按行数自动拆成多个 SQLite 文件，也可以不配置 logical partition，仅用 `__all__` + row segment 控制文件大小。
- **二级查询裁剪**：`select(where=...)` 先裁剪 logical partitions，再通过 segment `stats_json` 跳过无关物理文件。
- **写入分层**：`write(rows, mode=...)` 是高层主入口（契约感知），`insert_all()` 是低层基础（布尔标志），`buffered_writer()` 缓冲批量写入。
- **布局维护**：`layout_report()` 检查分区行数，`compact()` 整理 row segment，`repartition_to()` 可生成新的分区布局。
- **另存与统一迁移**：`save_as()` / `migrate_to()` 在 SQLite、Parquet 与 PostgreSQL 之间复制数据并保留源库，支持选表、改名和调整目标布局；先暂存、校验，再统一发布。
- **时序语法糖**：`Database.timeseries()` 是 `table()` 的薄封装（duration/calendar 分区 + order_key + time），适合 `query(start/end)`、`latest()`、`asof()` 等业务查询。
- **可视化管理台**：`iosql web -s <db>` 启动本地 Web 管理台（默认只读，`--write` 开启维护），支持数据浏览、契约/分区/segment 透视、explain 裁剪可视化与时序快览图表。

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

普通安装：

```bash
python -m pip install iosql
```

仓库开发安装：

```bash
python -m pip install -e .
```

需要 PostgreSQL 迁移或 PostgreSQL 引擎时安装可选依赖：

```bash
python -m pip install -e ".[postgresql]"
```

Parquet 后端随默认依赖安装，普通安装和开发安装均包含 DuckDB，无需额外 extra。

### Parquet 后端

```python
from iosql import Database, Segment

with Database("parquet://db/archive.iosql") as db:
    bars = db.timeseries(
        "bars", time="ts", pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "close": "float"},
        partition_every="1day", segment=Segment.rows(max_rows=500_000),
    )
    bars.write([{"symbol": "AAPL", "ts": 1735689600000, "close": 187.4}])
    rows = bars.query(symbols="AAPL", order_by="ts").to_list()
```

表契约与 manifest 仍保存在 `meta.sqlite3`，业务数据位于 `data/*.parquet`。
已有库可通过 `db.table("bars")` 直接重开，新增列在旧文件中读为 NULL。
SQLite 与 Parquet 之间可用 `db.migrate_to(target_uri, verify="row-count")`
转换，CLI 同样支持：

```bash
iosql migrate -s sqlite://db/market.iosql --target parquet://db/archive.iosql --verify row-count
iosql migrate -s parquet://db/archive.iosql --target sqlite://db/restored.iosql --verify row-count
```

#### 用 CLI 将 SQLite 另存为 Parquet

`migrate` 可以把整个 SQLite 目录库复制到独立的 Parquet 目录库，保留源库的业务数据和契约。以下是跨磁盘迁移 K 线库的示例；请根据实际环境调整路径，并选用尚不存在的目标目录：

```bash
iosql_source="/media/lsl/M3/finbase.crypto/binance/future_usdt/kline.sqlite-6h.iosql"
iosql_target="/media/lsl/Z1/finbase.crypto/binance/future_usdt/kline.parquet-6h.iosql"
iosql_work="/media/lsl/Z1/finbase.crypto/binance/future_usdt/.iosql-migration-work"

# 先预览：统计源库的行数和预计批次数，不创建目标库
iosql migrate \
  --source "sqlite://$iosql_source" \
  --target "parquet://$iosql_target" \
  --batch-size 50000 \
  --verify row-count \
  --dry-run

# 确认预览后执行另存，并在发布前比较源和目标的行数
iosql migrate \
  --source "sqlite://$iosql_source" \
  --target "parquet://$iosql_target" \
  --batch-size 50000 \
  --verify row-count \
  --temp-dir "$iosql_work"
```

`--temp-dir` 指定转换工作区，须位于目标库之外；示例把它放在目标盘，与目标代际位于同一文件系统，避免发布时跨盘复制。目标盘需预留工作区和目标数据所需的空间。源库和目标库不能相同或互相包含，`--verify row-count` 要求选中的目标表不存在或为空。

预览会统计源库全部选中表，数据量大时可能耗时；它不连接目标，因此不检查目标权限或已有表。正式迁移前应停止其他程序向源库写入，并关闭读取流、缓冲写入和未提交事务；迁移使用协作锁，不能约束直接操作 SQLite 文件或未接入协调协议的旧客户端。读取源库时可能产生协调锁、SQLite WAL 或 SHM 文件，但不会删除源业务数据。

正式命令成功后，检查 JSON 输出中的 `published: true`、`verified: true` 和相等的 `source_rows`、`target_rows`。行数校验不能替代逐行内容校验；迁移失败或中断时先检查错误和恢复状态，再决定如何重试。迁移后的副本使用 `parquet://` URI 打开。

Parquet 的 append/ignore 追加写为不可变数据片，通过原子清单发布，不重写历史完整行；
主键查重仍可能扫描历史键列。修改和删除会重写受影响的物理集合。
建议使用 `buffered_writer()` 和 `Segment.rows` 限制数据片数与单文件大小；
segment 表沿用现有 upsert/replace 限制。读取流存活期间，同表写入会等待；
同一线程写入前应先耗尽或关闭读取流。单个物理集合的清单原子切换，跨文件写入没有统一事务。

支持 Python 数据与维护接口、表重命名和 CLI `migrate`。其余 CLI 命令、Web
工作区发现、数据库目录重命名、SQLite PRAGMA、持久化索引、`commit=False`
与 `aggregate_by_bucket()` 暂不支持 Parquet。只接管 iosql 创建的目录库，
不会自动注册任意已有 Parquet 文件。

完整 Parquet 后端示例：[14_parquet_backend.py](examples/14_parquet_backend.py)。
SQLite → Parquet 单向迁移示例：[16_sqlite_to_parquet.py](examples/16_sqlite_to_parquet.py)，
覆盖 dry-run、行数校验和目标库查询。

构建 wheel：

```bash
python -m pip wheel . --no-deps -w dist
```

项目使用 `pyproject.toml` 作为打包入口，版本号来自 `iosql.version.__version__`。

## 快速开始

下面是核心 `Table` API。除 `partition=...` 外，它尽量保持 SQLite 表的心智：声明表、写入行、用 `where` 查询。

```python
from iosql import Database, Partition

with Database("sqlite://database/market.iosql") as db:
    ticks = db.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
    )

    result = ticks.write([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
    ])

    # select() 的 fields/where 均可省略；文本 where 支持 == 和 between
    rows = ticks.select(
        where="symbol == 'AAPL' and ts between 1735689600000 and 1735776000000",
        order_by="ts",
        to_dict=True,
    )

    plan = ticks.explain_select(
        where="symbol == 'AAPL' and ts between 1735689600000 and 1735776000000",
        order_by="ts",
    )

    print(result.rows_seen)
    print(rows)
    print(plan.scanned_partitions)
```

## 常用 API

### 重命名

```python
db.rename("market-renamed.iosql")        # SQLite 目录库改名
db.rename_table("ticks", "ticks_2026")   # 逻辑表改名并维护缓存
db.table("ticks_2026").rename("ticks")   # 也可直接在 Table 上改名
```

CLI：

```bash
iosql rename-database -s database/market.iosql --target market-renamed.iosql
iosql rename-table -s database/market.iosql -t ticks --new-table ticks_2026
```

SQLite table 重命名会移动表目录并更新表内 manifest；database 重命名会刷新当前 `Database` 实例 URI。普通库移动目录，已有代际发布的库使用快照发布并退休旧目录，需要复制数据与额外空间。覆盖已有目标时需要显式传 `overwrite=True`；CLI 对应 `--overwrite --yes`。`Database.rename(overwrite=True)` 会校验源/目标都是 iosql 目录库，拒绝根目录、家目录和符号链接目标；替换失败可回滚。

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
db.table("events", pk=["event_date", "name"], partition="calendar(event_date,every=1day,timezone=Asia/Shanghai)")
db.table("daily_bars", pk=["symbol", "date"], partition="calendar(date,every=4day,timezone=Asia/Shanghai)")
db.table("items", pk=["id"], partition="range(id,width=100000)")
```

字符串 partition 统一示范 `timezone=`（与对象 API 一致）；`tz=` 保留为兼容别名，解析结果相同。

时序数据优先使用 `duration(...)` 或 `calendar(...)`。`range(...)` 主要用于 id、offset、版本号等非时间数值字段；历史库中基于 `partition_key` 的 `range(...)` 可以作为兼容迁移方案。

`calendar(...)` 的 `every` 会真实参与分桶（如 `4day`/`1week`）；`fmt` 仅接受 `YYYY`/`YYYYMM`/`YYYYMMDD`/`YYYYMMDDHH`/`YYYYMMDDHHmm`，其他值抛 `PartitionSpecError`。duration 间隔支持别名、任意 `Nunit` 以及 `Nmonth`（按 30 天）/`Nyear`（按 365 天）。range/duration 分区键会把整数值浮点输入向下取整归一为整数（写入 `0.0` 的分区键为 `0`，用 `value=0` 可查询命中）。

等价对象 API：

```python
Partition.duration("ts", every="1day")
Partition.calendar("event_date", every="1day", timezone="Asia/Shanghai")
Partition.range("id", every=100000)
```

`timeseries()` 是 `table()` 的薄语法糖：duration 场景等价于 `Database.table(..., partition=Partition.duration(time, every=partition_every), order_key=time, time=time)`。主键推荐 `pk=` 拼写（`primary_key=` 兼容保留）：

```python
table = db.timeseries(
    "ticks",
    time="ts",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1hour",
)
```

calendar（日期字符串）场景直接传 `partition=Partition.calendar(...)`。以下旧参数已废弃（触发 `DeprecationWarning`，行为等价）：

| 废弃参数 | 迁移写法 |
| --- | --- |
| `partition_kind='calendar'` | `partition=Partition.calendar(time, every=..., timezone=...)` |
| `time_unit='date'` | 同上（使用 calendar 分区表达日期语义） |
| `target/min/max_rows_per_file` | `segment=Segment.rows(max_rows=..., target_rows=..., min_rows=...)` |

### 写入

```python
# 高层主入口：契约感知，mode 缺省回落表契约 conflict
table.write({"symbol": "AAPL", "ts": 1735689600000, "price": 187.4})
table.write(rows, mode="ignore")   # append / ignore / upsert / replace

with table.buffered_writer(max_rows=10_000) as buffer:
    buffer.append([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
    ])
```

`insert_all()` 是低层基础入口（布尔标志 `replace=`/`ignore=`，两者互斥），供进阶场景使用；日常写入统一使用 `write()`。

每行必须包含契约声明的全部主键字段；行缺失主键列会立即抛 `BadArgument`，不会退回 SQLite rowid 自增。
分区表使用 `upsert`/`replace` 时，分区列必须属于主键；否则没有跨文件全局主键索引，写入会明确报错。`append`/`ignore` 仍只保证各物理文件内的主键冲突语义，调用方须自行维护跨分区唯一性。

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
    partition="calendar(date,every=1year,timezone=Asia/Shanghai)",
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

with book.buffered_writer(max_rows=100_000) as buffer:
    for rows in stream_batches:
        buffer.append(rows)
```

默认 SQLite 配置是适合持续导入的 balanced 模式：`journal_mode=WAL`、`synchronous=NORMAL`、64 MiB page cache。需要更强断电保障时显式改为 `FULL`：

```python
db = Database(
    "sqlite:///media/lsl/G1/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
    max_process_connections=32,
    max_ordered_read_connections=128,
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
`max_connections_per_file` 是每个 SQLite 文件的连接池上限，
`max_process_connections` 是所有 mysqlite 连接池与有序 raw 读连接共享的进程级真实连接预算，
`max_ordered_read_connections` 是单次有序归并允许同时读取的分区数。
默认值分别为 `5`、`1`、`32` 和 `128`，由 iosql 在进程内强制执行，不依赖提高
`ulimit -n`。WAL 模式下每条
SQLite 连接可能同时占用数据库、WAL 和 SHM 文件描述符；包含两个元数据库时，
单表稳态 FD 预算可粗略按
`(max_open_partitions + 2) * max_connections_per_file * 3` 估算。

进程预算耗尽时会跨连接池回收空闲连接；大量分区扫描使用带租约的线程安全 LRU，
不会关闭其他线程正在使用的分区。`Database.close()` 或上下文管理器仍是
确定性释放资源的首选方式；低层清理注册表使用弱引用，遗弃对象不会再被全局注册表
保留。带 `order_by` 的查询若分区数超过 `max_ordered_read_connections` 或当前进程预算，
会直接抛 `BadArgument`；应收窄查询范围、提高预算或去掉全局排序。外部归并仍是后续能力。

同机协作 API 使用库级共享/独占租约：普通操作持共享租约；迁移、重分区、备份/恢复和重命名等操作申请独占租约。独占操作期间，新的协作 API 调用会立即抛出 `DatabaseBusy`；迁移通过 `lock_timeout` 等待正在进行的共享操作结束。同表写入仍由表级文件锁串行化。普通跨分区查询不保证统一数据快照，普通跨文件写入也没有崩溃原子性。原始文件访问、未接入协调协议的旧版客户端及多机共享 WAL 目录不受保护；详见[统一迁移与安全发布设计](docs/design/migration-api-20261018-unified.md)。

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
  --max-connections-per-file 1 \
  --max-process-connections 32
```

2026-06-22 在 G1（`/dev/sdb1`、ext4、USB HDD `Elements 25A1`）验证提交
`6326a4d`：默认 `ulimit -n=1024` 下，5000 分区写入/全扫描并连续遗弃 500 个
数据库实例，`fd_peak=41`、`fd_final=4`、`peak_open_partitions=5`，未出现
`Too many open files`。同环境 Linux 全量测试为 `263 passed, 14 skipped`。

2026-06-23 修复进程预算与并发 LRU 后再次在 G1 验证：进程预算 `16`，5000 分区
和 500 个遗弃实例下，真实连接峰值 `15`，FD `4 -> 48 -> 4`，分区峰值 `5`；
Linux 默认 `ulimit -n=1024` 最终全量测试为 `278 passed, 14 skipped`。

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
    {"field": "ts", "op": "between", "value": [1735689600000, 1735776000000]},
    {"field": "venue", "op": "==", "value": "NASDAQ"},
]
rows = table.select(
    where=where,
    order_by="ts desc",
    limit=100,
    to_dict=True,
)

plan = table.explain_select(where=where, order_by="ts desc", limit=100)
segments = table.segments()
```

`select(fields, where)` 的两个位置参数均可省略：`select()` 即全表扫描；`columns=` 是 `fields=` 的别名（与 `query(columns=)` 对齐），两者同传冲突报错。返回值语义：`select()` 急切求值并返回完整列表（默认 dict 行，`to_dict=False` 返回行元组）；`query()` 返回惰性 fluent 对象（`.to_list()` / `.iter_batches()` / `.explain()`）。`select_all()` 已废弃，等价于 `select(to_dict=True)`。

`select()` / `iter_select()` / `iter_select_batches()` 只接受底层参数（`fields`、`where`、`order_by`、`limit`、`columns`、`batch_size`）；传入 `symbols`/`start`/`end`/`date`/`session` 或其他未知参数会抛 `BadArgument` 而不是被静默忽略，业务过滤请改用 `query()` / `explain_select()`。

dict 条件的合法 op（别名自动归一化，非法 op 在构造时即抛带修复指引的 `BadArgument`）：

| 用户 op | 含义 | 内部别名 |
| --- | --- | --- |
| `==` | 等于 | `eq`、`=` |
| `!=` | 不等于 | `neq` |
| `>` / `>=` / `<` / `<=` | 比较 | `gt` / `ge` / `lt` / `le` |
| `between` | 闭区间（值为二元列表） | `between_and`、`range` |
| `in` | 集合（值为非空列表） | — |

`where` 支持 dict/list/`Case`，也支持受限文本表达式。文本表达式会被解析为内部条件，不会作为原始 SQL 拼接；当前支持比较、`and` / `or`、括号、`between` 和 `in`。

`segments()` 返回物理文件级 manifest，包含 logical partition、segment id、文件 key、行数、状态和列级 `stats`。`explain_select()` 同时返回 logical partitions 和 physical segments 的候选/扫描/跳过信息。compact 后默认只返回当前可见 segment；需要检查旧文件时可用 `segments(include_obsolete=True)`。

### Row Segment Compaction

金融 K 线、tick、orderbook 等 append-only 场景中，晚到数据会进入新的 active/delta segment。这样可以保证写入简单可靠，但同一时间范围可能需要扫描多个 segment。显式调用 `compact()` 可把当前可见 segment 按 `order_key` / time / 主键重新排序并写入新 generation：

```python
result = table.compact()
print(result.rows_read, result.rows_written, result.new_segments)

all_segments = table.segments(include_obsolete=True)
```

`compact()` 成功后会把旧 segment 标记为 `obsolete`，默认查询、`segments()`、`layout_report()` 和 `repartition_to()` 都只使用新 segment。`compact()` 本身不会物理删除旧 SQLite 文件；append-only 表中如果已经存在跨 segment 重复主键，compact 会把重复主键分到不同新 segment，保持现有查询结果不变。

compact 使用临时磁盘进行外部排序，行数据只保留读取批和一个目标 chunk，另保留 segment 发布元数据；应预留与分区规模相当的暂存空间。新文件全部写入后，一次 manifest 事务切换可见集合并记录发布代成员。写入或发布失败保留旧集合，进程退出遗留的不可见文件可由 `validate()` 发现，重试时安全复用。`validate()` 同时核对发布记录与可见文件，合法的跨文件重复主键不视为损坏。

SQLite 的 `create_index(column)` 持久保存逻辑定义，新分区、新 segment、compact 和 SQLite 目标迁移均继承；`drop_index(column)` 也删除该定义。SQLite/Parquet 的 latest 在物理文件内选出每组候选，Python 内存随组数增长；`upsert_all()` 只合并命中输入主键的行，缺字段保留旧值、显式 None 写 NULL。

manifest 引用的 segment 文件缺失时，读取路径直接抛 `UnsafeOperation`，不会隐式重建空文件。`validate()` 会反向扫描 data 目录并报告未被 manifest 引用的孤儿 segment 文件（`orphan_segment_file` 警告，不计入错误）；`compact()` 遇到目标文件名被历史孤儿文件占用时，会先安全清理孤儿再复用。

需要释放 compact 后的旧文件时，先执行深度校验，再清理 obsolete segment。`cleanup_obsolete()` 默认 dry-run，只预览候选文件和可释放字节；真正删除必须显式传 `dry_run=False`。删除前默认会校验当前可见 segment 的 SQLite integrity、row_count 和 manifest stats，校验失败会拒绝删除旧文件：

```python
validation = table.validate(deep=True)
assert validation.ok, validation.to_dict()

preview = table.cleanup_obsolete()
print(preview.bytes_reclaimable, preview.files)

cleanup = table.cleanup_obsolete(dry_run=False)
print(cleanup.files_deleted, cleanup.bytes_deleted)
```

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

`latest()` / `asof()` 用于金融截面查询，默认返回每个 symbol 截至 `as_of` 的最新一行；多 symbol 查询会按传入的 symbol 顺序返回。K 线、orderbook 等多维数据可传 `by=["symbol", "timeframe"]` 或 `by=["symbol", "venue"]`；`group_by=` 已废弃，请统一使用 `by=`。

### 交易日 / Session

`date=` 按时间列和时区解释为自然日。期货夜盘、跨时区行情等场景应显式声明交易日/session 列：

```python
ticks = db.timeseries(
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

rows = ticks.query(session="2026-01-06", symbols="AU").to_list()
snapshot = ticks.latest(
    session="2026-01-06",
    symbols="AU",
    as_of=1767661200000,
).one()
```

`session=` 会编译成对 `session_column` 的等值或 `in` 条件，可与 `symbols`、`start/end`、`date`、`where`、`latest()` 和 `asof()` 组合。新写入的 segment 会记录 `session_min/session_max`，`explain()` 可显示 `pruned_by_segment_session_range`。

### 流式查询和导出

大结果集优先使用批量迭代接口，避免 `select()` / `to_list()` 一次性持有全部行。`iter_batches()` / `iter_select_batches()` 是主形态；`iter_rows()` / `iter_select()` 是它的逐行语法糖，逐条处理时更顺手：

```python
for batch in ticks.query(start=start, end=end).iter_batches(batch_size=50_000):
    process(batch)

for row in table.iter_select(
    where={"field": "ts", "op": ">=", "value": start},
    batch_size=50_000,
    to_dict=True,
):
    process_one(row)
```

CLI `export` 会批处理写文件，适合导出多年 K 线、tick 或回测输入：

```bash
iosql export \
  -s sqlite://database/market.iosql \
  -t ticks \
  -o ticks.jsonl \
  --where '{"field":"ts","op":">=","value":1735689600000}' \
  --batch-size 50000
```

无全局 `order_by` 时，查询按分区/segment 分批读取。SQLite 的 `select()` 在全局 `order_by` 与 `limit` 同时提供时，会把 `ORDER BY ... LIMIT` 下推到每个 segment，再在 Python 层归并取前 `limit` 行。带排序的 `iter_select*()` / `query(...).iter_rows()` / `.iter_batches()` 使用分区只读游标和堆归并，惰性输出；归并受 `max_ordered_read_connections` 与进程连接预算约束。PostgreSQL 的排序在数据库端执行。

SQLite/Parquet 配置 `max_process_connections=1` 时，无序流会先将当前物理文件的查询结果写入临时文件并释放读取连接，然后向调用方交付批次，使处理批次时的读写和 `save_as()` 不会等待自身占用的配额。内存仍按批次使用，但首批要等待当前文件扫描完成，临时磁盘用量随该文件的查询结果增长；`limit` 会下推到文件查询。多个流或有序流占满进程连接预算时，新的连接申请会快速抛出 `BadArgument`，可关闭已打开的流或调大 `max_process_connections` 后重试。

读取流从开始消费到耗尽或关闭期间持有共享租约；提前退出循环时应显式关闭流，迁移前还需 flush 缓冲并退出 `buffered_writer` 上下文。完整示例见 [13_streaming_ordered_query.py](examples/13_streaming_ordered_query.py)。

CLI `import` 会批处理读取 JSONL/CSV 并写入既有契约表，适合 vendor 历史文件和离线回补包：

```bash
iosql import \
  -s sqlite://database/market.iosql \
  -t ticks \
  -f ticks.jsonl \
  --format jsonl \
  --batch-size 50000 \
  --mode append
```

CSV 导入会按表契约中的 `columns` 对 integer/float/bool 做基础类型转换；JSON 对象或对象数组仍兼容，但超大文件推荐使用 JSONL 或 CSV。

`iosql export` 先写临时文件、成功后再原子替换 `--output`，查询或写入失败时旧输出保持不变；`iosql import` 支持 UTF-8 BOM，JSONL 坏行与 CSV 字段数不匹配会报具体行号，中途失败会输出含 `rows_seen`/`rows_written`/`batches_committed` 的 JSON 汇总并以退出码 1 结束。JSON 输出中 bytes 列统一为 base64。

### 日历分区

如果数据使用日期字符串，可以直接使用 `Partition.calendar(...)`；对应 `timeseries()` 快捷版本见 `examples/02_calendar_events.py`：

```python
events = db.table(
    "events",
    pk=["event_date", "name"],
    columns={"event_date": "text", "name": "text", "city": "text"},
    partition=Partition.calendar("event_date", every="1day", timezone="Asia/Shanghai"),
)

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

compact_result = table.compact()
print(compact_result.rows_read, compact_result.rows_written)

validation = table.validate(deep=True)
print(validation.ok, validation.errors)

preview = table.cleanup_obsolete()
print(preview.bytes_reclaimable)

new_spec = table.spec().with_partition(partition="duration(ts,every=1hour)")
result = table.repartition_to("sqlite://database/market-hourly.iosql", spec=new_spec)
print(result.rows_copied, result.partitions_created)
```

CLI 整理 row segment：

```bash
iosql compact -s sqlite://database/market.iosql -t ticks
iosql validate -s sqlite://database/market.iosql -t ticks --deep
iosql segments -s sqlite://database/market.iosql -t ticks --include-obsolete
iosql cleanup-obsolete -s sqlite://database/market.iosql -t ticks        # dry-run
iosql cleanup-obsolete -s sqlite://database/market.iosql -t ticks --yes  # 执行删除
```

### 表说明、字段解释与声明值域

为表持久化"给人看和给 LLM 看"的元信息：表介绍、逐列解释、以及业务/模型约定的闭区间值域（`low`/`high`，只存契约，不做写入校验、不参与统计）。建表时可在 `db.table()` / `db.timeseries()` 传 `description` / `column_descriptions` / `column_ranges`；已有表用 `table.update_metadata(...)` 增补，或直接用 CLI：

```bash
# 读取规范 JSON 结构（表介绍 / 主键 / 逐列 类型·解释·值域；+inf/-inf 表示无穷边界）
iosql describe -s sqlite://database/market.iosql -t ticks

# 增量更新：省略的项不修改
iosql update-metadata -s sqlite://database/market.iosql -t ticks \
  --description "1 分钟行情，UTC 毫秒时间戳。" \
  --column-desc "price=未复权收盘价。" \
  --column-range "price=0:inf"

# 清除：--clear-description / --clear-column-descs / --clear-column-ranges，
# 或 --column-desc "price="（空文本清除该列解释）
```

Python 侧对应 `table.describe()` 与 `table.update_metadata(...)`，返回/输入均为 JSON 安全结构，适合程序与 LLM 直接消费。SQLite/Parquet 支持增量更新；PostgreSQL 可通过 `describe()` 读取已绑定契约中的元信息，不支持 `update_metadata()`。

### 另存副本与 engine 性能对比

`Database.save_as()` 把全库另存到目标 URI，`Table.save_as()` 只另存当前表。
两者都会保留源业务数据和契约，源对象仍指向原库；副本后续写入与源库互不影响。
已有的 `migrate_to()` 同样保留源库，`save_as()` 是表达另存意图的等价入口。

```python
with Database("sqlite://db/market.iosql") as source:
    result = source.save_as("parquet://db/comparison.iosql", verify="row-count")
    assert result.published and result.verified
    rows = source.table("bars").query(symbols="AAPL", order_by="ts").to_list()
```

只另存一张表可调用 `source.table("bars").save_as(target_uri)`；选表、改名用
`tables=["bars"]` 或 `tables={"bars": "archived_bars"}`，其余参数与 `migrate_to()` 一致。
目标已有数据时按 `mode` 处理主键冲突，不自动清空；做性能比较时建议新目标并开启
`verify="row-count"`，该校验要求选中目标表不存在或为空。

[另存与查询计时示例](examples/18_save_as_compare_engines.py) 保留 SQLite 和 Parquet 两份相同数据，
使用相同条件、投影、排序及完整结果消费方式，校验查询结果后比较首次查询与预热后中位耗时。
运行 `uv run --with-editable . python examples/18_save_as_compare_engines.py`。
另存耗时与查询耗时分开；首次查询不代表系统冷缓存，实际比较应使用自己的数据规模和查询负载。

### 统一迁移与调整目标分区

`Database.migrate_to()` 与 `Table.migrate_to()` 支持 SQLite、Parquet 和 PostgreSQL 之间的迁移，默认复用源契约，也可通过 `specs` 调整目标布局。源业务数据和契约保留；协调文件与临时工作区由库管理。CLI 的源库支持 SQLite/Parquet；以 PostgreSQL 为源时使用 Python API 并先绑定表契约。

```python
from iosql import Database

with Database("sqlite://database/market.iosql") as source:
    options = {
        "tables": ["daily_bars", "kline_1m"],  # 不传则迁移全库
        "batch_size": 50_000,
        "verify": "row-count",
    }
    target_uri = "postgresql://user:pass@localhost:5432/market"
    plan = source.migrate_to(target_uri, dry_run=True, **options)
    print(plan.source_rows, plan.batches)           # 预览不连接目标库

    result = source.migrate_to(target_uri, **options)
    print(result.published, result.rows_written, result.verified)
```

单表迁移：

```python
with Database("sqlite://database/market.iosql") as source:
    ticks = source.table("kline_1m")
    result = ticks.migrate_to(
        "postgresql://user:pass@localhost:5432/market",
        tables={"kline_1m": "pg_kline_1m"},
        batch_size=50_000,
    )
```

PostgreSQL 作为源库写回 SQLite 时，需要重新绑定同一份表契约：

```python
with Database("sqlite://database/market.iosql") as source:
    spec = source.table("kline_1m").spec()
with Database("postgresql://user:pass@localhost:5432/market") as pg:
    pg.table("pg_kline_1m", spec=spec)
    result = pg.migrate_to(
        "sqlite://database/from-postgresql.iosql",
        tables={"pg_kline_1m": "kline_1m"},
        verify="row-count",
        batch_size=50_000,
    )
```

CLI：

```bash
iosql migrate \
  -s sqlite://database/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --table kline_1m:pg_kline_1m \
  --batch-size 50000 \
  --verify row-count
```

也可以先执行 `iosql migrate ... --dry-run` 查看表、源行数和批次数估算。

Database 和 Table 都使用 `tables=None/列表/映射`：分别默认全库和当前表，改名统一使用 `{源表名: 目标表名}`。Table 旧 `target_table=` 与位置目标名、Database 的 `to_postgresql()` 保留兼容。

迁移时直接指定目标分区（`specs` 的键始终是源表名，未配置的表沿用源契约）：

```python
with Database("sqlite://database/market.iosql") as source:
    ticks = source.table("kline_1m")
    result = ticks.migrate_to(
        "parquet://database/hourly.iosql",
        tables={"kline_1m": "hourly_bars"},
        specs={"kline_1m": {"partition": "duration(ts,every=1hour)"}},
        verify="row-count",
    )
# 去掉目标分区：specs={"kline_1m": {"partition": None}}
# 完整契约也可使用：specs={"kline_1m": ticks.spec().with_partition(every="1hour")}
```

CLI 对应 `--specs '{"kline_1m":{"partition":"duration(ts,every=1hour)"}}'`，也支持 `--specs @布局.json`。布局可以修改 partition/segment，不会隐式改变列、主键或时间单位。已有目标需要与最终布局兼容；`mode="replace"` 仍是主键冲突策略，不能用于清空目标。

正式迁移全程禁止源、目标的协作读写，取得不到锁的普通 API 立即抛 `DatabaseBusy`；迁移等待独占锁的总时限由 `lock_timeout=30.0` 控制。请先关闭读取流、flush 缓冲并结束未提交事务或缓冲写入上下文。锁在持久发布后释放，临时数据清理在锁外进行。本地源的临时转换默认放在原库 `__iosql_migrations__/任务ID/`，PostgreSQL 源使用系统临时目录；可用 `temp_dir=` 或 `--temp-dir` 更改。目标侧另准备完整代际，保证先转换、后统一发布；中断后的所有读写都会检查恢复屏障。失败的暂存保留供检查。

旧内容在解锁前移入父目录协调空间的任务专属退休目录，锁外清理只删除该目录，避免误删原 URI 上后来新建的数据。合法空表可作为迁移目标，未写入表的当前实例声明在发布刷新后仍可用。库级协调层在 Windows 使用原生共享/独占文件锁；Windows 尚未进行原机和断电验证，不承诺 POSIX 目录 fsync 的同等持久性。

[安全迁移与路径示例](examples/17_safe_migration_and_paths.py) 演示大小写目录、SQLite→Parquet 迁移与备份、快照 junction 和库锁符号链接拒绝。运行 `uv run --with-editable . python examples/17_safe_migration_and_paths.py`；示例会清理自己的临时目录，平台能力不足时明确跳过对应场景。

`verify="row-count"` 要求所有选中目标表不存在或为空，在发布前比较真实行数。`dry_run=True` 只持源共享锁，不连接目标，不创建业务临时目录；它不会验证目标连接、权限或已有表兼容性。源协调锁文件可以产生，待恢复的源库会报错。多个独立查询跨越发布时间可能分别读到旧版和新版。

本地源和目标不能相同或互相包含，`temp_dir` 也不能位于目标库内。暂存和完整目标代际需要额外磁盘空间，已有目标的未选中表也会进入新副本。

结果保留 `rows_copied` 等旧字段，新增 `published`、`operation_id`、`rows_written` 和逐表 `source_spec/target_spec/partitions_created`。`rows_copied` 包括 ignore 成功处理但跳过的行，不能用它判断是否已经发布。失败异常包含 `partial_result`；发布状态不确定时不要盲目追加重试。原始文件访问和未接入协调协议的旧版客户端不受此锁保护。

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
     → iosql repartition --source DB --table daily_bars --partition 'calendar(date,every=4day,timezone=Asia/Shanghai)' --replace-source --yes
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
| `09_existing_database_append.py` | 独立模拟已有库，重新打开持久化契约后继续增量写入。 |
| `10_existing_database_repartition.py` | 独立模拟已有源库，重新打开后重分区到新目标库。 |
| `11_sqlite_to_postgresql_migration.py` | SQLite 源库 dry-run、迁移到 PostgreSQL、row-count 校验和目标侧查询。 |
| `12_sqlite_postgresql_roundtrip.py` | SQLite -> PostgreSQL -> SQLite 双向转换和 row-count 校验。 |
| [13_streaming_ordered_query.py](examples/13_streaming_ordered_query.py) | 跨分区全局排序，用 `query().iter_rows()` 惰性读取。 |
| [14_parquet_backend.py](examples/14_parquet_backend.py) | Parquet 分区、segment、重开与 SQLite 双向迁移。 |
| [15_postgresql_timeseries_query.py](examples/15_postgresql_timeseries_query.py) | PostgreSQL 时间窗、latest/asof、原生计划与复合索引。 |
| [16_sqlite_to_parquet.py](examples/16_sqlite_to_parquet.py) | SQLite → Parquet 预览、迁移、校验与目标查询。 |
| [17_safe_migration_and_paths.py](examples/17_safe_migration_and_paths.py) | 大小写路径、安全迁移与备份、链接路径拒绝。 |
| [18_save_as_compare_engines.py](examples/18_save_as_compare_engines.py) | `save_as()` 保留源库，校验副本后比较 SQLite/Parquet 相同查询的耗时。 |

各示例的运行条件和环境配置见[完整示例索引](examples/README.md)。

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
python examples/09_existing_database_append.py
python examples/10_existing_database_repartition.py
python examples/11_sqlite_to_postgresql_migration.py
python examples/12_sqlite_postgresql_roundtrip.py
python examples/13_streaming_ordered_query.py
python examples/14_parquet_backend.py
python examples/16_sqlite_to_parquet.py
uv run --with-editable . python examples/17_safe_migration_and_paths.py
uv run --with-editable . python examples/18_save_as_compare_engines.py
PYTHONPATH=. python examples/finance/01_kline_bars.py
```

`11_sqlite_to_postgresql_migration.py` 未设置 `POSTGRES_DSN` 时只执行 SQLite 源库准备和 dry-run；设置 `POSTGRES_DSN` 后会重建 PostgreSQL 示例表 `iosql_example_migration_ticks` 并执行真实迁移。
`12_sqlite_postgresql_roundtrip.py` 未设置 `POSTGRES_DSN` 时只执行 dry-run；设置后会演示 `sqlite -> postgresql -> sqlite` 双向转换。

低层 SQLite 工具示例位于 `examples/mysqlite/`，用于 `iosql.utils.mysqlite` 的 schema-on-write、批量写入、冲突处理和连接池清理。它们不是新的主 API 入口。

## 可视化管理台（Web）

iosql 自带一个本地 Web 管理台（只读浏览 + 受控维护），无需写代码即可查看表契约、分区布局、物理 segment 和数据样本。

安装 Web 可选依赖：

```bash
python -m pip install -e ".[web]"
```

`.[ui]` 仍是兼容安装别名。

启动（默认**只读模式**）：

```bash
iosql web -s database/market.iosql
# iosql Web: http://127.0.0.1:8686 (READ-ONLY)
```

开启可修改模式（写入与布局维护操作）：

```bash
iosql web -s database/market.iosql --write
```

其他参数：`--host`（默认 127.0.0.1）、`--port`（默认 8686）、`--no-browser`（不自动打开浏览器）。绑定具体非回环 IPv4/主机名时，该地址会加入 Host 白名单；绑定 `0.0.0.0` 时必须传一个或多个 `--allowed-host <访问地址>`。其余 Host 头仍被拒绝；网络暴露会打印警告。

功能分区：

| 工作区 | 只读模式 | 可修改模式 | 说明 |
| --- | :-: | :-: | --- |
| Overview | ✅ | ✅ | 全库概览：表清单、行数、分区数、segment 数、partition 策略 |
| Data | ✅ | ✅ | Browse 与 Query 合并后的工作区：条件构造器筛选、keyset 翻页、排序、Explain 裁剪可视化、时序查询、单元格详情、CSV/JSONL 导出 |
| Structure | ✅ | ✅ | 表契约 / 逻辑分区 / 物理 segment 三层透视 |
| Charts | ✅ | ✅ | 时序折线逐行流式绘制，不抽样；空 symbol 时每个标的分别成线，最多 32 个 symbol |
| Maintain | ❌ | ✅ | layout_report → compact → validate → cleanup_obsolete 步骤流；重分区 |

安全模型：只读模式下服务端**不注册任何写路由**（写端点不存在而非被拒绝），并只信任本机 Host（`localhost`/`127.0.0.1`/`[::1]`）；可修改模式下 truncate/delete/cleanup_obsolete/repartition 等危险操作均需手输表名确认（重分区无 dry-run 预览，确认后直接写入新库）。Data 的 keyset 分页在排序列含 NULL 时返回 422 并禁用该列排序，避免静默漏行。过滤计数的流式查询预算为 5000ms，超时返回 422；无筛选计数仍走 manifest 求和。定位是本机 localhost 工具，请勿暴露到公网。设计细节见 `docs/design/ui-20260823-overview.md`。

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

迁移发布后，以上表目录位于 `market.iosql/__iosql_generations__/任务ID/` 内，逻辑 URI 保持不变；父目录 `.iosql-locks/` 保存稳定锁及当前代际控制记录。应使用 `backup`/`restore` 或迁移接口复制数据库，不能仅搬动表目录或丢弃控制记录。

不配置 `partition` 时，所有数据使用 `__all__` 作为逻辑分区名。不配置 `segment` 时，每个逻辑分区仅一个文件。

## 注意事项

- 核心 `Database.table(...)` 不需要 `time` 字段；分区字段由 `Partition.*(...)` 指定。duration/calendar 分区在未显式声明 time 时会自动反推 time 契约（`range` 分区不反推），因此普通分区表也可直接使用 `query(start/end/date)`、`latest()`、`asof()`。
- `timeseries()` 是 `table()` 的薄语法糖；`partition_kind` / `time_unit` / `*_rows_per_file` 已废弃，迁移写法见"创建或打开表"一节的对照表。
- 多分区查询支持全局 `order_by` 和 `limit`；`select()` 在两者同传时会把排序与 limit 下推到各 segment 后再归并（内存有界），只排序不限行时结果仍会在 Python 层合并排序。大结果集排序导出可使用 `iter_select_batches(order_by=...)`：带排序时走跨 partition 的 k 路归并流式读取（每分区一条只读连接单游标，无 OFFSET 翻页，每分区只排序一次；limit 下推为每分区 LIMIT 走 top-N 排序；排序临时数据可落盘 temp_store=FILE）。Python 侧峰值驻留 ≈ 分区数 × 内部 fetch 粒度，总量钳制在约 10 万行。快照语义：迭代建立后本次迭代不可见新增分区/行（分区文件内为语句级读快照，跨分区非原子）。排序值序对齐 SQLite（ASC NULL 最小 / DESC NULL 最大），结果与 `select(order_by=...)` 一致；无排序导出优先使用 `iter_select_batches()` / `iter_batches()` 或 CLI `export --batch-size`。
- 大文件导入优先使用 JSONL/CSV 和 `iosql import --batch-size`；JSON 数组格式保持兼容但不推荐用于数 GB 文件。
- 配置 `Segment.rows(...)` 后，同一 logical partition 可以拆成多个物理 SQLite 文件；晚到数据会进入新的 active/delta segment，可用 `compact()` 显式整理为新的 sealed generation，再用 `validate(deep=True)` / `cleanup_obsolete()` 释放旧文件。
- `layout_report()` 只做诊断，不会修改原库；重分区请使用 `repartition_to()` 写入新的目标库。
- `columns={"col": "type"}` 中声明的列类型在自动建表时优先生效；未声明的列由数据推断。
- PostgreSQL URI 格式为 `postgresql://user:pass@host:port/dbname`；SQLite 到 PostgreSQL 迁移优先使用 `Database.migrate_to()` / `to_postgresql()` 或 `iosql migrate`，生产迁移建议先 `--dry-run`，再用 `--verify row-count` 校验。
- PostgreSQL 也可以作为迁移源写回 SQLite；重新打开 PostgreSQL 逻辑表时需要提供同一份 `TableContract`，见 `examples/12_sqlite_postgresql_roundtrip.py`。

### partition 一词三义对照表

`partition` 参数在不同方法中含义不同，使用时按下表区分：

| 语境 | 含义 | 示例 |
| --- | --- | --- |
| 建表 | **分区策略**（对象或字符串，声明如何路由） | `db.table(..., partition=Partition.duration("ts", every="1day"))` |
| 维护 | **分区键**（单个 logical partition 的 key，限制作用范围） | `table.compact(partition=<key>)`、`table.cleanup_obsolete(partition=<key>)` |
| 重分区 | **目标分区策略**（写入新库时的新布局） | `table.repartition_to(target_uri, spec=new_spec)` |

同一参数名承载三种语义是刻意保持的简写；需要精确控制作用范围时，`compact(partition=...)` / `cleanup_obsolete(partition=...)` 也可用 `partitions=` 别名传入分区键列表。

### 三后端支持面

| 能力 | SQLite | Parquet | PostgreSQL |
| --- | --- | --- | --- |
| 建表/契约 | `table` / `timeseries`，重开读取持久化契约 | 同 SQLite | `table` / `timeseries`；重开需显式绑定契约 |
| 写入 | `write` / `buffered_writer` / `insert_all` / `upsert_all` | 同 SQLite；append/ignore 写新数据片，修改重写命中集合 | `write` / `insert_all` / `upsert_all` |
| 条件查询 | `select` / `iter_select` / `iter_select_batches` | 同 SQLite | 同 SQLite，数据库端执行 |
| 时序查询 | `query` / `latest` / `asof` | 同 SQLite | 同名接口，数据库端执行 |
| 查询计划 | `explain_select` / 查询对象 `.explain()`，分区与 segment 裁剪 | 同 SQLite | 原生 JSON，支持 `analyze=True` |
| 索引 | 单列索引 | 无持久化索引 | 单列/复合 B-tree，安全删除、等价主键复用 |
| 数据维护 | `truncate` / `delete` / `delete_all` / `count_rows` | 同 SQLite | `delete` / `delete_all` / `count_rows` |
| 文件布局维护 | `segments` / `compact` / `validate` / `cleanup_obsolete` / `layout_report` / `repartition_to` | 同 SQLite | 无文件级维护 |
| 元数据 | `describe` / `update_metadata` | 同 SQLite | `describe` 读取绑定契约 |
| 迁移 | `migrate_to`，目标可为三种后端 | 同 SQLite | `migrate_to`；源表需绑定契约 |
| CLI / Web | 支持 | CLI 仅 `migrate`，Web 暂不支持 | CLI 仅迁移目标，Web 暂不支持 |

PostgreSQL 14+ 可作为在线时序存储。`query/latest/asof` 的筛选、排序、limit 和分组取最新值均由数据库执行。`query().iter_batches()` / `.iter_rows()` 使用命名游标；`select()` / `.to_list()` 会物化完整结果。start/end/as_of 包含边界，latest 保持请求 symbol 顺序；同时间按剩余主键升序决胜。指定 symbol 且有 symbol/time 前缀索引时，单分组 latest 使用 LATERAL LIMIT 1；其他分组使用 DISTINCT ON。单个 Table 使用一条连接，消费期间应先关闭流再执行其他会结束事务的操作。

```python
bars = db.timeseries('bars', time='ts', pk=['symbol', 'ts'],
                     columns={'symbol': 'text', 'ts': 'integer', 'close': 'float'},
                     partition_every='1day')
for batch in bars.query(symbols='AAPL', start=start_ms, end=end_ms,
                        columns=['ts', 'close'], order_by='ts').iter_batches(10_000):
    consume(batch)
plan = bars.query(symbols='AAPL', start=start_ms, end=end_ms).explain()
measured = bars.latest(symbols=['AAPL', 'MSFT'], as_of=end_ms).explain(analyze=True)
bars.create_index(columns=['symbol', 'ts'])  # 等价主键索引会被复用
```

`.explain()` 默认只规划，隐藏谓词常量；`analyze=True` 真实执行 SELECT 并提供耗时和 buffer，`raw_plan=True` 显式保留原始谓词与输出表达式，可能含业务常量。`QueryPlan.engine_plan` 保存结构化计划，分区列表使用 PG 关系名；默认 scanned_partitions 是计划访问关系，ANALYZE 时是 Actual Loops>0 的实际访问关系。segment 字段为空，过滤、排序和 limit 均在数据库执行。

`create_index(column=...)` 或 `create_index(columns=[...])` 显式创建父表 B-tree；子分区自动继承。`drop_index(columns=[...])` 只删除有 iosql 标记、定义与归属仍匹配的二级索引；约束和外部用户索引受保护。`upsert_all()` 只合并输入主键的字段，缺字段保留，显式 None 写 NULL，重复主键按输入顺序合并；不读取全表。`count_rows()` 保持精确计数，可能扫描整个表。

重开表会验证声明列的真实类型、主键顺序、分区形态与单列 RANGE 键。分区主键必须包含分区列。原生 date/timestamp/timestamptz 查询按契约时区绑定边界；calendar text 使用统一定长 compact 或无偏移 ISO 格式，查询精度独立于分区周期；带偏移时间使用 timestamptz。

运行示例：`POSTGRES_DSN=... uv run --extra postgresql examples/15_postgresql_timeseries_query.py`。查询基准：`POSTGRES_DSN=... uv run --extra postgresql benchmarks/benchmark_postgresql_query.py`，默认覆盖百万/千万行。结果和环境见 [基准报告](benchmarks/results/postgresql-query-20261002.md)。

本机已有 PostgreSQL 程序时，可先执行 `bash examples/start_postgresql_test.sh`，再直接运行 `p3 examples/15_postgresql_timeseries_query.py`（p3 是本机 python3 别名）。示例会自动读取本地测试连接文件，显式 POSTGRES_DSN 优先；重启电脑后重新执行启动脚本即可。详细配置、其他脚本的环境文件和停止命令见 [示例运行说明](examples/README.md#postgresql-本机查询示例)。

### 动态扩列（schema_policy='evolve'）语义

默认策略 `evolve` 允许写入时动态加列（`ALTER TABLE ADD COLUMN`，O(1)），语义契约如下：

1. **列集合单调增长**：只允许加列，不允许删列或改列名。新列补齐到所有既有分区文件，旧分区的缺失行读为 `NULL`（与 SQL `union all + null` 语义一致），`explain_select()` 不视为异常。
2. **契约回写**：动态加列成功后同步更新持久化契约的 `columns`，`spec().columns` 恒为当前实际 schema 的上确界——重开数据库（零仪式场景）不会漂移。
3. **类型固化与冲突**：未声明列首次写入时推断类型并固化进契约；同列后续数据类型不兼容时抛 `BadArgument`（错误信息含列名、已固化类型、本次类型与修复建议）。数值 int↔float 互通；数字写入 text 声明列由 SQLite TEXT affinity 转换放行。

### 导出面分层

- **用户层**（业务开发）：`Database` / `Partition` / `Segment` / `Where` + 结果类型（`WriteResult`、`QueryPlan`、`LayoutReport` 等）。业务建表优先使用 `db.table(...)` 快捷参数。
- **高级层**（迁移与诊断）：`TableContract` / `TableSpec` / `PhysicalLayout` / `TimeColumn`，用于跨库绑定契约、手写复杂契约等场景。
- `Case` 的运算符重载（`*`=and、`+`=or）属内部实现细节，文档与示例不示范；dict / 文本 where 是唯一推荐的条件表达形式。

### 生产迁移准备

1. 源表必须有持久化契约：`source.table("name").spec()` 不应为 `None`。
2. 先执行 `dry_run=True` 或 `iosql migrate --dry-run`，确认表名、源行数和批次数。
3. 使用新的目标表名或确认目标表为空，再执行全量迁移。
4. 对全量迁移启用 `verify="row-count"` 或 `--verify row-count`。
5. 先关闭读取流、flush 内存缓冲、退出缓冲写入上下文并结束未提交事务；正式迁移由库级独占租约阻止协作读写。原始文件访问和旧版客户端需另行停用。
6. PostgreSQL `table_names()` 只返回逻辑表；native partition 子表不需要用户直接操作。
7. `calendar(...)` 迁移支持 `text` / `date` / `timestamp` 目标列的 native range 边界；text 日期建议统一使用 ISO 日期（如 `2026-01-05`）或 compact 日期（如 `20260105`），不要在同一列混用格式。
8. 预留暂存与完整目标代际的磁盘空间；成功后检查 `published` 和 `verified`。失败时保留异常中的 `operation_id`、`partial_result`、`recovery_path`，先确认发布状态再重试。

- `Segment.rows` 表当前不支持 `upsert`/`replace` 覆盖写；`compact()` 不做去重，只整理 append-only 数据布局。需要覆盖写时请关闭 segment 或使用 `append`/`ignore`。

## 变更记录

- v0.3.0 - 用户接口统一（PLAN-052）：`Database.table()` 新增 `time=` / `symbol_column=` / `session_column=`；`timeseries()` 收敛为 `table()` 薄语法糖并补 `pk=` 别名，废弃 `partition_kind` / `time_unit` / `*_rows_per_file`（DeprecationWarning + 等价迁移）；duration/calendar 分区自动反推 time 契约，普通分区表直达 `query()` / `latest()` / `asof()`；`select()` / `iter_select()` / `explain_select()` 参数默认化并新增 `columns=` 别名；dict 条件 op 归一化白名单进 `Case` 构造入口（`between` 容错、非法 op 即时报 `BadArgument` 含修复指引，堵静默失效链）；evolve 动态扩列三语义落地（旧分区 NULL 补齐、契约回写、类型冲突报错）；`write()` 无契约错误附修复指引；`insert_all` 增加 replace/ignore 互斥校验；弃用 `select_all()` 与 `latest(group_by=)`；字符串 partition 规范输出统一为 `timezone=`（解析层 `tz=` 兼容）。
- v0.2.5 - 修复列类型推断 Bug（用户声明的 `columns` 类型此前被静默忽略）；修复 PostgreSQL URI 解析（此前不可达）；修复元数据查询 SQL 注入（5 处 f-string 改为参数化）；修复 `Partition | None` 在 Python 3.9 上的兼容性；修复 `create_table` 静默空操作；清理重复异常定义和死代码文件。
- v0.2.4 - 修复查询语义，重组 core/query 与 engines 包结构，删除旧 query/sqlite/postgresql 兼容入口。
- v0.2.1 - 引入时序表契约、布局诊断、重分区规划和 pyproject 打包入口。
- v0.2.0 - 新分区系统，支持 `DurationPartition` 和 `CalendarPartition`。
- v0.1.2 - 添加 `create_index`。
- v0.1.1 - 添加分区格式。
- v0.1.0 - 初始版本。
