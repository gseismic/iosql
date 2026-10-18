---
name: iosql-usage
description: This skill should be used when the user asks to use iosql, design partitioned tables, write or query time-series data, diagnose query plans, repartition, migrate between SQLite, Parquet, and PostgreSQL, use the Parquet backend, operate mysqlite, or mentions Database.table, Database.timeseries, Partition, Segment, iter_rows, repartition_to, or the iosql CLI.
---

# iosql 使用指南

iosql：支持本地 SQLite、Parquet 与 PostgreSQL 的时序数据存储库。版本契约 v0.3.x，接口已定稿冻结（长期稳定），本文件与该契约同步。

## 30 秒上手（80% 的场景就这一段）

```python
from iosql import Database

with Database("sqlite://db/market.iosql") as db:        # 必须 sqlite:// 前缀；with 自动 close
    bars = db.timeseries(
        "kline_1m",
        time="ts",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "open": "float", "close": "float"},
        partition_every="1day",                        # 按天自动分区
    )

    result = bars.write([{"symbol": "AAPL", "ts": 1735689600000, "open": 187.0, "close": 187.4}])
    assert result.rows_written == result.rows_seen     # 检查写入结果

    rows = bars.query(symbols="AAPL", start=1735689600000, end=1735776000000, order_by="ts").to_list()
    plan = bars.query(symbols="AAPL", start=1735689600000, end=1735776000000).explain()  # 分区裁剪分析
    snap = bars.latest(symbols=["AAPL"], as_of=1735776000000, by=["symbol"]).to_list()  # 最新截面
```

## 决策树：选对入口

批量归档或列式分析可使用 `Database("parquet://db/archive.iosql")`。DuckDB 随 iosql
默认依赖安装，无需额外 extra。建表、查询和迁移沿用主 API；Parquet 会重写受影响的数据文件，
适合批量写入。先看 [Parquet 使用指南](references/parquet.md) 了解性能和接口边界，再运行
[SQLite → Parquet 迁移示例](examples/16_sqlite_to_parquet.py) 或
[Parquet 后端完整示例](examples/14_parquet_backend.py)。CLI 目前只有 `migrate` 支持 Parquet URI。

```
带时间戳/日期的数据（K线、tick、orderbook、财报、新闻）
  → db.timeseries(name, time=..., pk=..., columns=..., partition_every="1day")

任意可排序字段分区（毫秒 ts / 日期字符串 / 序号 offset）
  → db.table(..., partition="duration(ts,every=1day)")
     # 日期串用 "calendar(date,every=1day,timezone=Asia/Shanghai)"
     # 非时间数值用 "range(id,width=100000)" 或 Partition.duration/calendar/range 工厂

只按行数滚文件（日志类，无逻辑分区）
  → db.table(..., segment=Segment.rows(max_rows=N), order_key=...)

直接操作单个 SQLite 文件（非 iosql 目录库）
  → iosql.utils.mysqlite.Database   # 低层工具，见 examples/mysqlite/
```

分区推荐统一字符串表达（CLI/诊断/API 复用）；`Partition.duration(...)` 等对象形式等价兼容。

## 执行工作流：先判断问题，再选择示例

1. 先判断数据是否有稳定的时间/日期字段。时序、行情、财报和事件优先选择
   `Database.timeseries()` 或显式 `Database.table(..., time=..., partition=...)`；无时间语义的
   日志、序号和 offset 选择 `range` 或纯 `Segment.rows`。
2. 再判断读取规模和排序要求。小结果用 `.to_list()`；结果可能很大时用 `.iter_batches()`；
   需要逐行处理或按全局字段排序时用 `query(..., order_by=...).iter_rows()`，不要先调用
   `select()` 再自行切片。
3. 写入后先检查 `WriteResult`，再用 `explain()` / `explain_select()` 验证分区和 segment
   是否被裁剪；只有计划与数据量符合预期时才进入性能调优。
4. 发现文件过多、分区倾斜或晚到数据后，先运行 `layout_report()` / `doctor`，再用
   `repartition_to()` 写入新库；迁移到其他后端时可通过 `migrate_to(specs=...)` 同时调整目标布局。
   不要把重分区当作原地修复，也不要在未确认源库可恢复前删除源库。
5. 跨后端迁移先 dry-run，再使用不存在或为空的目标表执行 `verify="row-count"`。
   迁移前关闭读取流、flush 缓冲并退出缓冲写入上下文、结束未提交事务；完成后检查
   `published` 和 `verified`。dry-run 不连接目标，不能据此确认目标权限或已有表兼容。
   直接操作单 SQLite 文件时进入 mysqlite 示例。

这条工作流对应主线示例的递进关系：01/02 解决建模入口，03 解决批量写入和计划确认，
04～08 解决布局与运维，09/10 解决已有库，11/12 解决迁移，13 解决大结果集的有序流式读取，
14 展示 Parquet 归档，15 展示 PostgreSQL 查询，16 聚焦 SQLite 到 Parquet 的迁移，17 展示迁移发布和路径安全。

## 参数怎么设：先估算数据量，再定分区

### 第 1 步：估算数据量（动手前必做）

- **每个逻辑分区落 10~50 万行**是经验值：太小 → 大量小文件、打开/切换开销大；太大 → 单文件查询慢、裁剪粒度粗。
- 每日行数先算出来：`标的数 × 每标的每天条数`（K线）或 `采样频率 × 86400/间隔秒`（tick）。

### 第 2 步：选分区类型并定粒度

| 类型 | 分区键 | 粒度参数 | 适用 |
|------|--------|---------|------|
| `duration(ts, every=1day)` | 毫秒时间戳 | `every`：1hour / 1day / 1week / 1month | 行情、tick、orderbook |
| `calendar(date, every=1day, timezone=...)` | 日期字符串 | `every`：1day / 7day / 1month + `timezone` | 交易日、财报、事件 |
| `range(id, width=100000)` | 任意数值 | `width` = 每分区数值跨度 | 序号、offset、版本号 |

粒度按每日行数定（以 duration 为例）：

- 每天 10~50 万行 → `every=1day`（日线、分钟线大多数情况）
- 每天 > 50 万行（orderbook/tick）→ `every=1hour` 或按交易所/venue 加进 pk 前先看是否够裁剪
- 每天 < 10 万行（低频事件）→ `every=1week` / `1month`，避免几千个小文件；日线历史回灌甚至 `every=1year`（20 年 = 20 个文件，而不是 5000 个）
- `calendar` 的 `timezone` 决定日期边界，跨时区市场必须显式传
- `range` 的 `width` 是数值区间宽度；连续唯一整数 id 时才近似等于每分区行数。

### 第 3 步：segment（单个物理文件大小）

- `segment=Segment.rows(max_rows=…)` 控制分区内物理文件滚动；不设则单分区单文件持续增长。
- 从 `max_rows=1_000_000` 开始测；宽表降到 20 万~50 万，窄表可提高。
- `target_rows`/`min_rows` 是 compact 的目标/合并下限，一般不用动。
- 高频宽表传 `stats_columns=["ts","sequence"]`，只对裁剪关键列维护 min/max（未统计列仍可查，但计划保守扫描）。

### 其他参数

- `pk`：唯一标识一行，时序一般 `["symbol", "ts"]`；写入每行必须带齐全部 pk 字段。
- `columns`：声明类型（text/integer/float/blob），声明优先于推断，避免二次打开时 `SchemaConflict`。
- `order_key`：segment 内排序键，时序传 `"ts"`（`timeseries()` 自动带）；写入顺序尽量与之一致。
- `symbol_column`：需要 `symbols=`/`latest(symbols=)` 的普通表必须显式传（`timeseries()` 默认 `"symbol"`）。
- `session_column`：期货夜盘等按交易日归属时传交易日列名。
- 写入批次：先测 `50_000` 行/批（HDD 上至少数千行，不要逐条 `write()`）。

### 不确定时

写入后跑 `table.layout_report()`，看 `report.recommendation`（行数倾斜、文件过多/过大会给建议）；调整分区用 `spec().with_partition(...)` + `repartition_to()` 迁新库。

## 场景导航（频率按真实示例反推）

### 常用（高频）

| 场景 | 最小做法 | 可运行示例 |
|------|---------|-----------|
| 建表 + 写 + 时间窗查 | 上面的 30 秒上手 | [examples/01_quickstart_timeseries.py](examples/01_quickstart_timeseries.py) |
| 日期字符串数据（ISO/交易日） | `db.timeseries(..., partition=Partition.calendar("date", every="1day", timezone="Asia/Shanghai"))`，查询 `query(date="2026-01-06")` | [examples/02_calendar_events.py](examples/02_calendar_events.py) |
| 大批量缓冲写入（万行级） | `with table.buffered_writer(max_rows=10_000) as buf: buf.append(batch)` | [examples/03_buffered_ingest_and_query_plan.py](examples/03_buffered_ingest_and_query_plan.py) |
| 大表控制文件大小 | `segment=Segment.rows(max_rows=500_000, target_rows=200_000)` | [examples/finance/02_orderbook_snapshots.py](examples/finance/02_orderbook_snapshots.py) |
| 大结果集全局排序 | `query(order_by="open_time").iter_rows(batch_size=...)`，先消费首行再继续迭代 | [examples/13_streaming_ordered_query.py](examples/13_streaming_ordered_query.py) |
| 金融最新截面 | `table.latest(symbols=[...], as_of=..., by=["symbol","timeframe"], columns=[...]).to_list()`；单条用 `.one()` | [examples/finance/01_kline_bars.py](examples/finance/01_kline_bars.py) |
| 任意条件查询 | `table.select(where=..., order_by=..., limit=..., to_dict=True)`；where 用 dict/list/文本/`Case` | [examples/01_quickstart_table.py](examples/01_quickstart_table.py)、[references/usage-patterns.md](references/usage-patterns.md) #Where 条件构造 |
| 表说明、字段解释与声明值域 | `table.describe()`；SQLite/Parquet 用 `update_metadata(...)` 增量更新，值域只描述、不校验写入 | [references/usage-patterns.md](references/usage-patterns.md#表说明与字段元数据)、[references/api-reference.md](references/api-reference.md#表说明与字段元数据) |
| 查询裁剪诊断 | `table.explain_select(where=..., order_by=...)` 或 `query(...).explain()` | [examples/03_buffered_ingest_and_query_plan.py](examples/03_buffered_ingest_and_query_plan.py) |
| 期货/夜盘交易日 session | 建表传 `session_column="trading_date"`，查询 `query(session="2026-01-06")` | [references/usage-patterns.md](references/usage-patterns.md) #交易日session |
| 打开已有库继续写 | `db.table("name")` 不重新声明契约；`write(mode="ignore")` 跳过重复主键 | [examples/09_existing_database_append.py](examples/09_existing_database_append.py) |
| Parquet 批量归档/分析 | `Database("parquet://...")` 配合分区、segment 和流式有序查询 | [references/parquet.md](references/parquet.md)、[examples/14_parquet_backend.py](examples/14_parquet_backend.py) |

### 进阶（偶尔需要）

| 场景 | 入口 | 可运行示例 |
|------|------|-----------|
| 事件/新闻/财报等低频表 | `db.timeseries(time=日期或ts, pk=..., segment=Segment.rows(target_rows=300_000))` | [examples/finance/03_financial_reports.py](examples/finance/03_financial_reports.py)、[04_news_announcements.py](examples/finance/04_news_announcements.py) |
| 晚到数据 + 纯 row segment | `db.table(..., segment=Segment.rows(max_rows=N), order_key=...)`，晚到数据进新 active segment | [examples/06_row_segment_late_data.py](examples/06_row_segment_late_data.py) |
| 布局诊断 + 重分区 | `layout_report()` → `spec().with_partition(...)` → `repartition_to(新库URI, spec=)`（只写新库） | [examples/04_layout_report_and_repartition.py](examples/04_layout_report_and_repartition.py) |
| CLI 重分区 | `iosql repartition -s ... -t ... --partition 'duration(ts,every=1hour)' --target 新库` | [examples/07_partition_string_repartition_cli.py](examples/07_partition_string_repartition_cli.py) |
| 跨后端迁移与调整目标布局 | `migrate_to(uri, specs=..., verify="row-count")`；正式迁移前先 dry-run | [examples/11_sqlite_to_postgresql_migration.py](examples/11_sqlite_to_postgresql_migration.py)、[examples/16_sqlite_to_parquet.py](examples/16_sqlite_to_parquet.py)、[references/api-reference.md](references/api-reference.md) |
| PostgreSQL 在线时序查询、计划与复合索引 | `query/latest/asof`、`.explain(analyze=True)`、`create_index(columns=[...])` | [examples/15_postgresql_timeseries_query.py](examples/15_postgresql_timeseries_query.py) |
| SQLite ⇄ PostgreSQL 双向 | PG 侧重开逻辑表需 `db.table(名字, spec=同一份契约)` | [examples/12_sqlite_postgresql_roundtrip.py](examples/12_sqlite_postgresql_roundtrip.py) |
| SQLite ⇄ Parquet 迁移 | `db.migrate_to("parquet://...")` 或反向迁移；正式迁移可启用 `verify="row-count"` | [examples/16_sqlite_to_parquet.py](examples/16_sqlite_to_parquet.py)、[references/parquet.md](references/parquet.md) |
| 逻辑分区 + 物理 segment 组合 | partition 与 segment 同时配置，explain 看两级裁剪 | [examples/05_partition_segments_smoke.py](examples/05_partition_segments_smoke.py) |
| 已有库整体重分区 | 读 `spec()` 构造新 spec → `repartition_to(..., batch_size=50_000)` | [examples/10_existing_database_repartition.py](examples/10_existing_database_repartition.py) |

### 高级（很少用，需要时按链接查）

| 主题 | 链接 |
|------|------|
| CLI 全部命令 + 安全规则（doctor/compact/import/export/backup/restore/rename/migrate/index） | [references/cli.md](references/cli.md) |
| Parquet 存储、写入成本、查询、迁移和能力边界 | [references/parquet.md](references/parquet.md) |
| 可视化管理台 `iosql web` 工作区/安全模型 | [references/ui.md](references/ui.md) |
| compact / cleanup_obsolete / validate / segments 维护流 | [references/usage-patterns.md](references/usage-patterns.md) #布局和重分区；错误对照 [references/troubleshooting.md](references/troubleshooting.md) #分区维护 |
| 流式查询 iter_select_batches / iter_batches / 大结果导出 | [references/usage-patterns.md](references/usage-patterns.md) #流式查询和导出 |
| upsert_all 字段级合并、delete/truncate、create_index/drop_index、rename | [references/usage-patterns.md](references/usage-patterns.md) |
| where 全部运算符、Case/CaseExpression 组合 | [references/api-reference.md](references/api-reference.md) #Where 条件支持的运算符 |
| mysqlite 单文件（schema-on-write/事务/加删列/索引/备份） | [examples/mysqlite/](examples/mysqlite/)、[examples/mysqlite-scenarios/](examples/mysqlite-scenarios/) |
| 写入性能调优（批次/PRAGMA/FD 预算/HDD） | [references/write-performance.md](references/write-performance.md) |
| 全部 API 签名、结果类型、异常、mysqlite 契约 | [references/api-reference.md](references/api-reference.md) |
| 迁移发布状态、锁超时、恢复及路径错误 | [references/troubleshooting.md](references/troubleshooting.md#统一迁移与协作锁)、[examples/17_safe_migration_and_paths.py](examples/17_safe_migration_and_paths.py) |
| 本仓库开发守则（接口冻结/PG 引擎差异/测试要求/反模式全表） | [references/design-contract.md](references/design-contract.md) |

## 示例目录：按问题选择

主线示例位于 `examples/`，每个文件解决一类可复用问题：

| 示例 | 解决的问题 |
| --- | --- |
| `01_quickstart_table.py` | 显式使用 `Table + Partition` 建立普通时间分区表，并用 `select` / `explain_select` 查询。 |
| `01_quickstart_timeseries.py` | 用 `timeseries()` 快速完成同类时间戳建模，理解 helper 与底层 API 的关系。 |
| `02_calendar_events_table.py` | 处理 ISO 日期字符串和时区边界，使用 `Partition.calendar`。 |
| `02_calendar_events.py` | 用 `timeseries()` 快速创建日期字符串事件表。 |
| `03_buffered_ingest_and_query_plan.py` | 对万行级写入使用 `buffered_writer`，同时检查分区/segment 裁剪。 |
| `04_layout_report_and_repartition.py` | 发现布局不合适后，诊断并将数据写入新的分区布局。 |
| `05_partition_segments_smoke.py` | 组合逻辑分区和物理 row segment，验证两级查询计划。 |
| `06_row_segment_late_data.py` | 无逻辑分区时按行滚动文件，并观察晚到数据与 stats pruning。 |
| `07_partition_string_repartition_cli.py` | 复用字符串 partition 表达，在 CLI 中执行安全重分区。 |
| `08_doctor_time_first_suggestions.py` | 对历史表运行 doctor，获得时间优先建模建议。 |
| `09_existing_database_append.py` | 重新打开已有库，沿用持久化契约继续增量写入。 |
| `10_existing_database_repartition.py` | 不改动源库，将已有表离线重分区到新目标库。 |
| `11_sqlite_to_postgresql_migration.py` | 先 dry-run，再迁移到 PostgreSQL 并做行数校验。 |
| `12_sqlite_postgresql_roundtrip.py` | 演示 SQLite → PostgreSQL → SQLite 双向转换。 |
| `13_streaming_ordered_query.py` | 对跨分区结果全局排序，并用 `query().iter_rows()` 惰性消费大结果集。 |
| `14_parquet_backend.py` | 演示 Parquet 分区存储、segment、流式排序、重开与 SQLite 双向迁移。 |
| `15_postgresql_timeseries_query.py` | 演示 PG14+ 时间窗流式查询、latest/asof、原生计划、复合索引与字段更新。 |
| `16_sqlite_to_parquet.py` | 演示 SQLite 源库 dry-run、迁移到 Parquet、row-count 校验和目标查询。 |
| `17_safe_migration_and_paths.py` | 演示大小写路径保留、迁移与备份，以及快照 junction/库锁符号链接拒绝。 |

金融示例位于 `examples/finance/`：`01_kline_bars.py` 解决 K 线 OHLCV，
`02_orderbook_snapshots.py` 解决高频 orderbook 快照，`03_financial_reports.py` 解决财报
日期和月度日历分区，`04_news_announcements.py` 解决新闻/公告的类型过滤与查询计划。

低层 mysqlite 示例位于 `examples/mysqlite/` 和 `examples/mysqlite-scenarios/`：
`mysqlite/01_schemaless_crud.py` 覆盖 schema-on-write CRUD，`02_bulk_query_and_conflict.py`
覆盖批量查询和冲突策略，`03_connection_pool_cleanup.py` 覆盖连接池清理；
`mysqlite-scenarios/01_crud.py`、`02_add_drop_column.py`、`03_query_variants.py`、
`04_rename_and_table_info.py`、`05_index_operations.py`、`06_backup.py` 分别覆盖六类
单文件运维问题。进入低层示例前，先确认需求不是普通 `Database.table()` /
`Database.timeseries()` 能解决的主 API 场景。

运行示例前先执行 `python -m pip install -e .`，此安装已包含 DuckDB 和 Parquet 后端。
脚本默认把可重复生成的数据库放在各自
`examples/runtime/` 下。未设置 `POSTGRES_DSN` 时，11 和 12 只执行 SQLite 准备与 dry-run。

本机 PostgreSQL 查询示例 15 可先执行 `bash examples/start_postgresql_test.sh`，
再直接运行 `python3 examples/15_postgresql_timeseries_query.py`。启动脚本复用已安装的
PostgreSQL 14+，保留独立的本机测试库和连接文件；15 优先使用 POSTGRES_DSN，
未设置时读取本地连接文件。其他 PG 示例/基准可先 source
`examples/runtime/postgresql-test.env`；重启电脑后重新运行启动脚本。

## 常用 API 速查

| 类别 | API | 要点 |
|------|-----|------|
| 入口 | `Database(uri)` | `sqlite://`、`parquet://` 或 `postgresql://` 前缀必带 |
| | `db.table(name, pk, columns, partition=, segment=, order_key=, symbol_column=, session_column=)` | 核心建表入口 |
| | `db.timeseries(name, time=, pk=, partition_every=)` | 时序语法糖（table 的薄封装） |
| | `db.table_names()` / `db.close()` | 列表 / 关闭 |
| 写入 | `table.write(rows, mode=None)` | mode: None（契约默认）/ append / ignore / upsert / replace |
| | `with table.buffered_writer(max_rows=N) as buf: buf.append(batch)` | 缓冲批量写 |
| | `table.truncate(confirm=True)` / `table.delete(where)` | 清空 / 条件删除 |
| 查询 | `table.query(symbols=, start=, end=, date=, session=, where=, order_by=, limit=)` → `.to_list() / .one() / .explain() / .iter_rows() / .iter_batches()` | 时序表（duration/calendar 分区表也可用） |
| | `table.select(fields="*", where=None, order_by=, limit=, to_dict=True)` | 所有表；默认返回 dict |
| | `table.explain_select(...)` | 查询计划（partition/segment 两级裁剪） |
| | `table.latest(symbols=, as_of=, by=, columns=)` | 最新截面；别名 `asof()` |
| 诊断 | `table.spec()` / `table.table_parts()` / `table.segments()` / `table.count_rows()` | 契约 / 逻辑分区 / 物理 segment / 行数 |
| | `table.describe()` / `table.update_metadata(...)` | 表与列的业务说明；更新仅 SQLite/Parquet 支持 |
| 维护 | `table.layout_report()` / `table.repartition_to(uri, spec=)` / `table.migrate_to(...)` | 诊断 / 重分区 / 迁移单表 |
| | `db.migrate_to(uri, tables=, specs=, verify=)` | SQLite/Parquet/PG 跨后端迁移，可调整目标布局 |

## 关键规则（违反即抛异常）

- URI 必须 `sqlite://` / `parquet://` / `postgresql://` 前缀，直接传文件路径报 `InvalidUriSchema`。
- Parquet 写入成本、读取流生命周期、支持的维护操作和后端差异见 [Parquet 使用指南](references/parquet.md)。
- 每行必须含完整 pk 字段，缺列抛 `BadArgument`（不会退回 rowid 自增）。
- 已存在的表用 `db.table("name")` 获取引用；重新声明不同契约抛 `TableSpecConflict`。
- `select()`/`iter_select*()` 不接受 `symbols/start/end/date/session`（抛 BadArgument）；业务过滤用 `query()`。
- `select()` 默认返回 dict 行；要 tuple 显式 `to_dict=False`。
- `query(symbols=)`/`latest(symbols=)` 要求建表时显式 `symbol_column="symbol"`（timeseries 默认已带）。
- 顶层导入只导出工厂与结果类型：用 `Partition.duration(...)`，不要 `from iosql import DurationPartition`。
- 废弃参数不要在新代码使用：`partition_kind`/`time_unit`/`*_rows_per_file`/`group_by`/`select_all`/`delete(None)`。
- `repartition_to()`/`migrate_to()` 写入目标布局，保留源表业务数据与契约；源库可产生协调文件和迁移工作区。
- `specs` 按已选源表名配置目标布局；`mode="replace"` 只处理主键冲突，不清空目标。
- 迁移以 `published` 确认正式发布；`rows_copied` 可包含 ignore 跳过的行。失败时记录 `operation_id`、`partial_result` 和 `recovery_path`，发布状态不确定时先查明状态再重试，见错误排查资料。
- 缓冲写入异常退出会发未 flush 行数的 RuntimeWarning——不要吞掉（意味着数据未落库）。
- `upsert`/`replace` 模式当前不支持 `Segment.rows` 表。
- SQLite/Parquet 目录分区表使用 `upsert`/`replace` 时，分区列必须在主键中；否则没有跨文件全局主键索引，入口会拒绝写入。`append`/`ignore` 的唯一性只在物理文件内保证。
- 同机协作 API 使用库级共享/独占租约：普通操作持共享租约；迁移、重分区、备份/恢复和重命名等操作持独占租约。独占期间新的协作 API 调用抛 `DatabaseBusy`，迁移用 `lock_timeout` 等待现有共享操作结束。同表写入仍由表级文件锁串行化；普通跨分区查询没有统一数据快照，普通跨文件写入没有崩溃原子性。原始文件访问、旧版客户端和多机共享 WAL 目录不受保护，详情见 [references/design-contract.md](references/design-contract.md#统一迁移与安全发布plan-083)。
- `query(..., order_by=...).iter_rows()` / `.iter_batches()` 保持惰性有序流式读取，不先调用 `select()` 全量物化；跨分区归并受 `max_ordered_read_connections` 和进程连接预算约束，超限抛 `BadArgument`。

## 报错先查

出错时直接把异常名/信息对照 [references/troubleshooting.md](references/troubleshooting.md)：契约冲突（`TableSpecConflict`/`SchemaConflict`）、参数错误（`BadArgument`/`PartitionSpecError`）、分区未裁剪、写入静默跳过、URI 错误、迁移问题等均有"错误信息 → 修复方法"对照。

## 详细资料（按需打开）

- [references/usage-patterns.md](references/usage-patterns.md) —— 全量代码模式（四种组合、写入模式、where 构造、布局维护、迁移、PG 引擎、mysqlite、测试建议）
- [references/parquet.md](references/parquet.md) —— Parquet 后端安装、存储方式、写入/读取语义、迁移和能力边界
- [references/api-reference.md](references/api-reference.md) —— 全部 API 签名、结果/查询/异常类型、常量、mysqlite 完整契约
- [references/troubleshooting.md](references/troubleshooting.md) —— 错误排查对照表
- [references/write-performance.md](references/write-performance.md) —— 大批量写入性能（回灌/orderbook/PRAGMA/FD/HDD）
- [references/cli.md](references/cli.md) —— CLI 命令与安全规则
- [references/ui.md](references/ui.md) —— 可视化管理台 iosql web（旧名 iosql ui）
- [references/design-contract.md](references/design-contract.md) —— 接口契约、设计偏好与开发守则（本仓库开发必读）
- [examples/README.md](examples/README.md) —— 主线 `01~17` 示例索引，每个示例对应一个常见问题；
  `examples/13_streaming_ordered_query.py` 专门覆盖有序大结果集的惰性读取。
- [examples/finance/README.md](examples/finance/README.md) —— K线、orderbook、财报、新闻四类金融场景。
- [examples/mysqlite/README.md](examples/mysqlite/README.md) 和
  [examples/mysqlite-scenarios/README.md](examples/mysqlite-scenarios/README.md) —— 低层单文件
  工具与六类运维场景。

本文件所在目录是 skill 根目录；`references/` 和 `examples/` 下的相对路径均相对于该目录解析。
示例脚本是 skill 的完整可运行副本，修改仓库根目录 `examples/` 后必须同步对应副本并做
逐文件一致性检查。
