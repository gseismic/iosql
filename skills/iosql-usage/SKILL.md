---
name: iosql-usage
description: 使用 iosql 构建、审查或调试本地 SQLite 时序数据存储。Use when working with iosql APIs such as Database, table, TableSpec/TableContract, timeseries, Partition (duration/calendar/range), Segment, write, write_buffer, query, latest, asof, select, explain_select, segments, validate, cleanup_obsolete, layout_report, repartition_to, rename, rename_table, spec, table_parts, delete, truncate, create_index, drop_index, Case, CaseExpression, Where, WriteResult, QueryPlan, LayoutReport, iosql CLI commands, iosql ui 可视化管理台, or iosql.utils.mysqlite; also use when designing financial time-series schemas, partition strategies, ingestion workflows, layout maintenance, CLI maintenance workflows, or tests for this repository.
---

# iosql 使用指南

> 版本对齐：iosql v0.3.x（PLAN-052~058 后的用户接口定稿版）。参数拼写与默认值已冻结（长期稳定），本文与该契约同步。

## 核心判断

优先使用主 API：

```python
from iosql import Database
```

只有在明确需要直接操作单个 SQLite 文件、schema-on-write、动态列或连接池细节时，才使用低层模块：

```python
from iosql.utils.mysqlite import Database
```

## 决策树：选对入口

```
需要逻辑分区 + 物理 segment？
├─ 是 → Database.table(..., pk=..., partition=..., segment=..., order_key=...)
│      适用于：任意可排序字段分区（时间戳、序号、offset 等）
│
需要 timeseries 便利查询（start/end/date/latest/asof）？
├─ 是 → Database.timeseries(...)
│      适用于：K线、tick、orderbook、新闻等带时间的金融数据
│
只需要纯 row segment 滚动（按行数拆文件）？
├─ 是 → Database.table(..., segment=Segment.rows(max_rows=N))
│      适合日志、事件等不需要逻辑分区、只需控制文件大小的场景
│
需要直接操作单个 SQLite 文件？
└─ 是 → iosql.utils.mysqlite.Database
       适合：独立 SQLite 工具、schema-on-write 快速原型
```

## Partition 与 Segment 的关系

`partition`（逻辑分区）和 `segment`（物理分段）是独立的两级机制，组合使用形成不同存储模式：

```
Partition（逻辑分区）           Segment（物理分段）
按业务键路由到不同目录         在同一逻辑分区内按行数拆分 .sqlite3 文件
e.g. ts=1735689600000          e.g. max_rows=500_000
```

### 四种组合及行为

| Partition | Segment | 行为 | 目录结构 |
|:---------:|:-------:|------|---------|
| ✅ | ✅ | 按分区键路由，每个分区内按行数自动滚动文件 | `partition_key.sqlite3` / `partition_key-1.sqlite3` |
| ✅ | ❌ | 按分区键路由，每个分区仅一个文件（无限增长） | `partition_key.sqlite3` |
| ❌ | ✅ | 全部数据写入 `__all__`，仅按行数滚动文件 | `__all__-0.sqlite3` / `__all__-1.sqlite3` |
| ❌ | ❌ | 退化为单文件 SQLite | `table_name.sqlite3` |

### 查询裁剪

有 `segment` 时执行**二级裁剪**：

1. **一级**：按 `partition` 键跳过不相关的逻辑分区
2. **二级**：通过 segment `stats`（每列 min/max）和 `session_min/session_max` 跳过不相关的物理文件

`explain_select()` 返回的 `QueryPlan` 同时展示 `scanned_partitions` / `scanned_segments` 和对应的 `skip_reasons`。

### 核心代码逻辑

```python
# do_insert_all 中的关键决策
part_obj    = partition or self._partition      # 有逻辑分区？
use_segment = bool(spec and spec.segment)       # 有物理分段？

if part_obj is None:
    logical_part = DEFAULT_LOGICAL_PART if use_segment else None  # "__all__" or None
else:
    partitioned_data = {part_obj.get_key(row): [rows...]}         # 按分区键拆散

for part_key, part_data in partitioned_data.items():
    if use_segment:
        _do_insert_segmented(...)   # segment 感知：检查行数、seal、滚动
    else:
        _do_insert_part(...)        # 直接写入对应 SQLite 文件
```

## API 速查表

| 类别 | API | 说明 |
|------|-----|------|
| **入口** | `Database(uri)` | 创建/打开数据库，`sqlite://` 或 `postgresql://` URI |
| | `Database.table(name, **opts)` | 创建/获取表（核心入口） |
| | `Database.timeseries(name, time=..., pk=..., **opts)` | 时序语法糖：table() 的薄封装（废弃参数迁移见下） |
| | `Database.rename(new_uri_or_name)` / `Database.rename_table(old, new)` / `table.rename(new)` | 重命名 SQLite 目录库或逻辑表；table 重命名会更新 manifest |
| | `Database.migrate_to(target_uri, tables=None, verify=None, dry_run=False)` / `to_postgresql(target_uri)` | 迁移 SQLite iosql 库到目标库 |
| **写入** | `table.write(rows, mode=None)` | 写入行；默认使用表契约 `conflict`，新表默认 append |
| | `table.write_buffer(mode=None, max_rows=...)` | 缓冲批量写入（上下文管理器） |
| | `table.insert_all(rows, ...)` | 低层写入（需明确传 pk/partition） |
| | `table.upsert_all(kv_list, pk)` | 按主键合并写入（merge 字段） |
| | `table.delete(where)` | 条件删除（传 None 已废弃，改用 truncate） |
| | `table.truncate(confirm=True)` | 清空表（需显式确认） |
| **查询** | `table.query(...).to_list()` / `.explain()` | 时序高层查询（timeseries 表；duration/calendar 分区的普通表也可用） |
| | `table.query(...).iter_batches(batch_size=...)` | 时序查询批量迭代 |
| | `table.select(fields="*", where=None, order_by, limit, to_dict=True, columns=)` | 底层条件查询（所有表）；fields/where 可省略，columns= 是 fields= 别名；**默认返回 dict 行**（`to_dict=False` 才返回 tuple） |
| | `table.iter_select(...)` / `iter_select_batches(...)` | 底层流式查询（所有表，默认 to_dict=True） |
| | `table.select_all(fields, where)` | 已废弃：等价于 select(to_dict=True)，触发 DeprecationWarning，永久保留 |
| | `table.explain_select(fields, where, order_by, limit, symbols=, start=, end=, date=, session=)` | 查询计划/分区裁剪分析；业务参数与 query() 对齐 |
| | `table.latest(symbols, as_of, by, columns)` | 最新截面查询；要求契约声明 symbol_column（见反模式） |
| | `table.asof(...)` | latest 的别名 |
| **诊断** | `table.spec()` | 获取当前表契约 |
| | `table.segments(include_obsolete=False)` | 获取物理 segment 清单 |
| | `table.table_parts()` | 获取逻辑分区列表 |
| | `table.validate(deep=False, include_obsolete=False)` | 校验 manifest 与物理 segment 文件 |
| | `table.layout_report()` | 分区诊断报告（行数、推荐） |
| | `table.compact(partition=None, target_rows=None)` | 显式整理 row segment，旧 segment 标记 obsolete；与写入共用锁串行化 |
| | `table.cleanup_obsolete(partition=None, dry_run=True, validate=True)` | 安全清理 compact 后的 obsolete 文件 |
| | `table.count_rows()` | 统计当前可见行数（无参，双引擎一致）；SQLite 按物理 segment 执行 `count(*)` |
| | `table.migrate_to(target_uri, target_table=None, verify=None, dry_run=False)` | 迁移单表到目标库 |
| | `table.repartition_to(target_uri, spec)` | 按新策略重分区到新库 |
| **维护** | `table.create_index(column, order, exist_ok=True)` | 为所有分区创建索引 |
| | `table.drop_index(column)` | 删除所有分区上的列索引 |
| | `Database.table_names()` | 列出数据库中所有表 |
| | `db.close()` | 关闭所有连接 |

## CLI 速查表

CLI 入口：

```bash
iosql --help
python -m cli --help  # 源码树内兼容入口，真实实现位于 iosql.cli
```

CLI 运维命令的 `--source` 只支持 SQLite 目录库，可传本地路径或 `sqlite://...` / `sqlite3://...`。如果 source 输入 `postgresql://...` 等其他 URI，应直接拒绝，不要自动包装成 SQLite 路径。`iosql migrate --target` 例外，目标可传 `postgresql://...`。

| 类别 | 命令 | 说明 |
|------|------|------|
| **发现** | `iosql tables -s DB` | 列出逻辑表 |
| | `iosql ui -s DB [--write] [--host] [--port] [--no-browser]` | 启动本地 Web 管理台（默认只读，详见下节） |
| | `iosql rename-database -s DB --target NEW_DB` | 重命名 SQLite 目录库；覆盖目标需 `--overwrite --yes` |
| | `iosql rename-table -s DB -t OLD --new-table NEW` | 重命名逻辑表；覆盖目标需 `--overwrite --yes` |
| | `iosql spec -s DB -t TABLE` | 输出表契约 JSON |
| | `iosql layout -s DB -t TABLE` | 输出 `layout_report()` |
| | `iosql parts -s DB -t TABLE` | 输出逻辑分区 |
| | `iosql segments -s DB -t TABLE` | 输出物理 segment |
| | `iosql validate -s DB -t TABLE --deep` | 深度校验 manifest、SQLite 文件和 stats |
| **查询** | `iosql explain -s DB -t TABLE --where JSON` | 输出 `explain_select()` 查询计划 |
| | `iosql query -s DB -t TABLE --where JSON --format jsonl` | 查询小规模结果 |
| **数据进出** | `iosql import -s DB -t TABLE -f rows.jsonl --mode append --batch-size 50000` | 批处理读取并写入既有契约表 |
| | `iosql export -s DB -t TABLE -o rows.jsonl --where JSON --batch-size 50000` | 批处理导出查询结果 |
| **诊断维护** | `iosql doctor -s DB [-t TABLE] [--fix]` | 检查布局和主键索引；`--fix` 只修索引；重分区建议会优先推荐业务时间列 |
| | `iosql compact -s DB -t TABLE [--partition PART]` | 整理 row segment，生成新 generation |
| | `iosql cleanup-obsolete -s DB -t TABLE [--yes]` | dry-run 或确认清理 obsolete segment 文件 |
| | `iosql migrate -s DB --target postgresql://... [--dry-run] [--verify row-count]` | 迁移 SQLite iosql 库到 PostgreSQL |
| | `iosql repartition -s DB -t TABLE --partition 'duration(ts,every=1hour)' --target TARGET` | 按新分区策略写入目标库 |
| | `iosql backup -s DB --target BACKUP` | 目录级备份 |
| | `iosql restore --backup BACKUP --target DB` | 目录级恢复；覆盖目标需 `--replace --yes`，且目标必须是含 `meta.sqlite3` 的 iosql 目录库（拒绝根/家目录） |
| | `iosql index list/check/create/drop ...` | 检查、创建或删除物理分区索引 |

### CLI 安全规则

- `repartition` 默认必须提供 `--target` 或 `--replace-source`，不会隐式替换源库。
- 需要替换源库时必须使用 `--replace-source --yes`；`--dry-run` 不写文件，所以不要求 `--yes`。替换过程原子化：备份用 `os.replace` 改名（近零成本），跨文件系统传 `--backup` 会明确拒绝（EXDEV，源库原封不动），失败自动回滚。
- `restore` 覆盖已有目标时必须使用 `--replace --yes`。
- `cleanup-obsolete` 默认 dry-run；只有传 `--yes` 才删除文件，删除前默认执行当前可见 segment 深度校验。
- `import` 使用 `table.write()`，目标表必须已有 `TableSpec/TableContract`；JSONL/CSV 按 `--batch-size` 流式读取，CSV 会按契约列类型做基础转换。
- `query` 适合排查和小规模输出到终端；`export` 使用 `--batch-size` 批处理写文件，适合无全局排序的大结果集导出。
- 复杂 where 优先传 JSON 或 `@file`，例如 `--where '{"field":"symbol","op":"==","value":"AAPL"}'`。

## 可视化管理台（iosql ui）

本地 Web 管理台：只读浏览 + 受控维护，无需写代码即可查看表契约、分区布局、物理 segment 和数据样本。依赖属于可选 extra：

```bash
python -m pip install -e ".[ui]"   # fastapi + uvicorn
iosql ui -s database/market.iosql           # 默认只读模式, http://127.0.0.1:8686
iosql ui -s database/market.iosql --write   # 开启维护操作
```

其他参数：`--host`（默认 127.0.0.1）、`--port`（默认 8686）、`--no-browser`。Python 侧入口是 `iosql.ui.create_app(source_uri, mode="readonly"|"write")`（惰性导入，未装依赖时抛带安装指引的 ImportError）。

| 工作区 | 只读 | 可修改 | 说明 |
|:--|:-:|:-:|---|
| Overview | ✅ | ✅ | 全库概览：表清单、行数、分区数、segment 数、partition 策略 |
| Browse Data | ✅ | ✅ | 表头每列过滤行（裸词精确 / `>` 比较 / `a~b` 范围 / 逗号多值，防抖即输即查）+ 条件构造器（AND/OR，与表头条件叠加）+ keyset 翻页 + 列头排序 + 时序表默认最新优先 + 毫秒时间戳可读化（可开关）+ 单元格详情（可一键以此值过滤）+ CSV/JSONL 导出 |
| Structure | ✅ | ✅ | 表契约 / 逻辑分区 / 物理 segment 三层透视 |
| Query | ✅ | ✅ | where 查询 + explain 裁剪可视化；时序表支持 start/end/date/session/latest 截面 |
| Charts | ✅ | ✅ | 时序快览折线图（服务端降采样） |
| Maintain | ❌ | ✅ | layout_report → compact → validate → cleanup_obsolete 步骤流；重分区 |

安全模型：

- 只读模式下服务端**不注册任何写路由**（写端点不存在而非被拒绝）。
- 可修改模式下 truncate/delete/cleanup_obsolete/repartition 等危险操作均需预览 + 手输表名确认。
- 定位是本机 localhost 工具，**不要暴露到公网**；排查 UI 问题时优先怀疑模式没开对（只读看不到 Maintain）。

## 工作流

### 1. 明确数据模型

确定时间列、主键、列类型、自然日还是交易日/session、预期单日或单分区数据量。

### 2. 声明表契约

两种入口等价：

**显式版（推荐）**：
```python
from iosql import Database, Partition, Segment
db = Database("sqlite://db/market.iosql")
ticks = db.table(
    "ticks",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition="duration(ts,every=1day)",
    segment=Segment.rows(max_rows=500_000),
    order_key="ts",
)
```

`partition` 推荐使用统一字符串表达，便于 CLI、诊断输出和 Python API 复用：

```python
"duration(ts,every=1day)"
"calendar(event_date,every=1day,timezone=Asia/Shanghai)"
"calendar(date,every=4day,timezone=Asia/Shanghai)"
"range(id,width=100000)"
```

时序表优先使用 `duration(...)` 或 `calendar(...)`。`range(...)` 主要用于 id、offset、版本号等非时间数值字段；历史库里的 `range(partition_key,...)` 可以作为兼容迁移方案。对象 API 仍然兼容：`Partition.duration(...)`、`Partition.calendar(...)`、`Partition.range(...)`。

**快捷版（timeseries helper）**：
```python
ticks = db.timeseries(
    "ticks",
    time="ts",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    partition_every="1day",
)
```

`timeseries()` 是 `table()` 的薄语法糖；calendar 场景传 `partition=Partition.calendar(...)`。废弃参数迁移：`partition_kind='calendar'` / `time_unit='date'` → `partition=Partition.calendar(...)`；`target/min/max_rows_per_file` → `segment=Segment.rows(...)`（触发 DeprecationWarning，行为等价）。

### 3. 选择分区策略

| 分区类型 | API | 适用场景 | 分区键格式 |
|---------|-----|---------|-----------|
| duration | `"duration(ts,every=1hour)"` | 毫秒时间戳 | 整数（对齐边界） |
| calendar | `"calendar(date,every=1day,timezone=Asia/Shanghai)"`、`"calendar(date,every=4day,timezone=Asia/Shanghai)"` | 日期字符串 | `YYYYMMDD` 等 |
| range | `"range(seq,width=10000)"` | 任意可排序数值 | 整数（对齐边界） |

### 4. 写入

```python
# 单条/多条写入
result = table.write([
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
])

# 缓冲批量写入（推荐大量数据时使用）
with table.write_buffer(max_rows=10_000) as buf:
    for batch in data_source:
        buf.append(batch)
# 退出上下文时自动 flush；块内抛异常且 buffer 非空时发 RuntimeWarning
# 提示未 flush 行数——不要吞掉这个警告，否则会静默丢数据

# 写入模式
# mode=None：使用表契约 conflict，新表默认 append
# mode="append"：严格追加，重复主键通常由 SQLite 约束处理
# mode="ignore"：重复主键跳过
# mode="upsert"/"replace"：覆盖写；当前不支持 Segment.rows 表
```

大批量写入建议：

- 日线历史回灌从每批 `50_000` 行开始测，按年分区通常优于按日创建大量小文件。
- orderbook/tick 使用 append-only，建议每批 `50_000` 至 `100_000` 行，并配置 `Segment.rows(max_rows=...)`。
- 高频宽表可用 `Segment.rows(..., stats_columns=["ts", "sequence"])` 限制 min/max 统计开销；partition/time/order/session 字段会自动保留。
- iosql 默认 SQLite 写入配置是 `WAL + synchronous=NORMAL`；更强断电保障可传 `Database(..., pragma_dict={"journal_mode": "wal", "synchronous": "full"})`。
- 主 API 默认 `max_open_partitions=5`、`max_connections_per_file=1`、`max_process_connections=32`，分别限制单表分区缓存、单文件连接池和进程真实连接总数；不要依赖提高 `ulimit -n`，也不要直接修改私有字段。
- 分区 LRU 使用租约保护并发访问；同一逻辑表仍建议单 writer，多线程读取可按需提高 `max_connections_per_file`，但总数受进程预算约束。
- 长期进程必须使用上下文管理器或显式 `db.close()`；弱引用清理只负责避免遗弃对象被注册表永久保活。
- 目标盘必须运行仓库 `benchmarks/benchmark_write.py`，不要用系统盘测试结果推断 ext4 HDD。
- 批次和磁盘比较使用 `benchmarks/sweep_write.py`，可同时传多个 `--root` 并生成 JSON/Markdown 排名。
- 多分区 FD 压力测试使用 `benchmarks/stress_open_files.py`，报告系统 FD 限制、进程 FD 峰值和分区连接峰值。
- 详细选择和命令见 [write-performance.md](references/write-performance.md)。

### 5. 查询

**时序查询（timeseries 表）**：
```python
# 时间窗口查询
rows = ticks.query(start=..., end=..., symbols="AAPL").to_list()

# 查询计划（分区裁剪分析）
plan = ticks.query(start=..., end=...).explain()
print(plan.scanned_partitions, plan.skipped_partitions)
```

**底层条件查询（所有表）**：
```python
where = [
    {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
    {"field": "ts", "op": "between", "value": [start, end]},
]
# select 默认 to_dict=True（双引擎一致），返回 dict 行；要 tuple 需显式 to_dict=False
rows = ticks.select(where=where, order_by="ts", limit=100)
plan = ticks.explain_select(
    where=where, order_by="ts",
    symbols=["AAPL"], start=start, end=end,   # 业务参数与 query() 对齐
)

# 大结果集批量读取
for batch in ticks.iter_select_batches("*", where=where, batch_size=50_000):
    process(batch)
```

**金融截面查询**：
```python
# 最新值（每个 symbol 截至 as_of 的最新行）
latest = ticks.latest(
    symbols=["AAPL", "MSFT"],
    as_of=1735776000000,
    by=["symbol", "timeframe"],
    columns=["symbol", "timeframe", "ts", "price"],
).to_list()

# 单条获取
row = ticks.latest(symbols="AAPL", as_of=ts).one()
```

### 6. 交易日 / Session

期货夜盘、跨时区行情使用 `session_column`：

```python
futures = db.timeseries(
    "futures_ticks",
    time="ts",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "trading_date": "text", "price": "float"},
    session_column="trading_date",
)
rows = futures.query(session="2026-01-06", symbols="AU").to_list()
```

### 7. 布局诊断和重分区

```python
report = table.layout_report()
print(report.total_rows, report.recommendation)

result = table.compact()
print(result.rows_read, result.rows_written, result.new_segments)
validation = table.validate(deep=True)
print(validation.ok, validation.errors)
preview = table.cleanup_obsolete()
print(preview.bytes_reclaimable)
print(table.segments(include_obsolete=True))

new_spec = table.spec().with_partition(partition="duration(ts,every=1hour)")
result = table.repartition_to("sqlite://db/hourly.iosql", spec=new_spec)
print(result.rows_copied, result.partitions_created)
```

### 8. SQLite 迁移到 PostgreSQL

安装 PostgreSQL 可选依赖：

```bash
python -m pip install "iosql[postgresql]"
```

Python：

```python
source = Database("sqlite://db/market.iosql")
result = source.to_postgresql(
    "postgresql://user:pass@localhost:5432/market",
    tables=["daily_bars", "kline_1m"],  # 不传则迁移全库
    batch_size=50_000,
    verify="row-count",                 # 可选：校验源/目标行数
)
print(result.rows_copied, result.batches, result.verified)
source.close()
```

迁移前预览，不连接目标库：

```python
plan = source.to_postgresql(
    "postgresql://user:pass@localhost:5432/market",
    dry_run=True,
    batch_size=50_000,
)
print(plan.source_rows, plan.batches)
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

也可以用 `iosql migrate ... --dry-run` 输出表、源行数和批次数估算。

### 9. 清理

```python
# 关闭（推荐）
db.close()

# 上下文管理器（自动关闭）
with Database("sqlite://db/test.iosql") as db:
    ...
```

## 设计偏好

- **接口已定稿冻结（PLAN-054，长期稳定）**：不再改参数名、默认值、方法职责；新增能力只能以"新方法/新参数"叠加。参数词汇表唯一拼写：`pk` / `columns` / `every` / `timezone` / `between` / `mode`；别名永久保留：`primary_key` / `fields` / `partition_every` / `tz` / `between_and` / 契约侧 `conflict`。缩写只允许行业级 `pk`，其余参数一律全称。结果对象字段冻结，`to_dict()` 是唯一序列化入口，新字段只能追加。
- **废弃参数只加不减**：`partition_kind`/`time_unit`/`*_rows_per_file`/`group_by`/`select_all` 的 DeprecationWarning 永久保留，不要计划移除，也不要在新代码里使用。
- 让 `TableSpec/TableContract` 成为表契约来源，避免隐式依赖第一次写入推断出的结构。
- 查询 API 优先表达业务条件：`symbols/start/end/date/session/where/order_by/limit`。
- 复杂 where 优先使用受支持的 dict/list/`Case` 或文本 where，不要拼接原始 SQL。
- **导出面收缩**：`from iosql import ...` 只导出工厂 `Partition.*` 和结果类型；`DurationPartition`/`CalendarPartition`/`RangePartition` 需从 `iosql.partition.duration` 等内部模块导入（顶层导入会 ImportError）。
- **symbol_column 显式化**：`db.table()` 默认 `symbol_column=None`；未声明时 `query(symbols=)`/`latest(symbols=)` 抛带修复指引的 `BadArgument`。需要 symbol 语义就显式声明 `symbol_column="symbol"`（timeseries() 保持默认 `"symbol"`）。`latest(by=...)` 显式分组不受影响。
- `select()` 双引擎统一默认 `to_dict=True` 返回 dict 行；依赖 tuple 行为的旧代码必须显式 `to_dict=False`。
- 间隔词汇统一由 `iosql.partition.duration.parse_interval_ms()` 解析：`Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')` 行为一致，支持别名 + 任意 `Nunit`。
- 代码结构按层理解：`iosql.core.query` 是通用查询 AST/where 解析；`iosql.engines.sqlite` / `iosql.engines.postgresql` 是引擎实现；旧 `iosql.query` / `iosql.sqlite` / `iosql.postgresql` 包已删除。
- `segment` 支持 `max_rows`/`target_rows`/`min_rows` 三级参数和可选 `stats_columns`，合理配置以控制文件大小及统计开销。
- `compact()` 只整理当前可见 row segment，生成新的 sealed generation，并把旧 segment 标记为 `obsolete`；默认查询和 `segments()` 不扫描 obsolete；需要释放旧文件时先 `validate(deep=True)`，再用 `cleanup_obsolete(dry_run=False)`。
- **单 writer 保证强化**：写入路径与 `compact()` 共用 `_compact_lock` 串行化，消除 compact 后向旧 segment 插入的复活竞态；compact 与写互斥是预期吞吐代价。多线程读取可按需提高 `max_connections_per_file`。
- 大结果集读取优先用 `iter_select_batches()` / `query(...).iter_batches()` 或 CLI `export --batch-size`；带全局 `order_by` 的跨 segment 查询仍可能需要 Python 层合并排序。
- 大文件导入优先用 `iosql import --batch-size` 读取 JSONL/CSV；JSON 数组兼容但不适合数 GB 文件。CLI import 输出的 `rows_written` 在 ignore 模式下如实输出 null（不冒充 rows_seen）。
- `repartition_to()` 写入新库，不要假设它会原地修改源库。
- SQLite 到 PostgreSQL 迁移优先用 `Database.migrate_to()` / `to_postgresql()`；迁移按 `batch_size` 逐批读取 SQLite 物理文件，适合海量 K 线，不会一次性载入全库。生产迁移建议先 `dry_run=True` 预览，再用 `verify="row-count"` 校验源/目标行数。
- PostgreSQL 也可以作为迁移源写回 SQLite；重新打开 PostgreSQL 逻辑表时需要提供同一份 `TableContract`，见 `examples/12_sqlite_postgresql_roundtrip.py`。PG 流式导出使用命名游标（单语句快照），无序翻页不会跳行/重复。
- PostgreSQL native partition 下，`duration(...)` / `range(...)` 使用数值边界；`calendar(...)` 支持 `text` / `date` / `timestamp` 目标列，text 日期列不要混用 ISO 与 compact 格式。
- 测试必须覆盖写入结果、查询结果和必要的 `explain()` 裁剪信息。
- 修改核心行为时，至少跑相关 sqlite 契约测试、partition 测试和 mysqlite 测试。
- `columns={"col": "type"}` 中声明的列类型在自动建表时优先生效；未声明的列由数据推断。
- 异常体系统一使用 `IosqlError` 子类（`BadArgument`、`TableSpecConflict`、`SchemaConflict` 等）；`IosqlBadArgument` 已移除，统一用 `BadArgument`。
- mysqlite 定位是"SQL 完备工具"（第三方库心智）：`insert_all(commit=False)` 显式拒绝（延迟提交与连接池归还语义冲突）；`select(limit=0)` 按 SQL 语义返回 0 行；错误路径统一回滚并转换异常；建表冲突抛类型化 `DuplicateColumn`/`PrimaryKeyNotInColumn`。mysqlite 无内置 rename/drop_column/backup——用原生 SQL 或连接 API 完成（见 `examples/mysqlite-scenarios/`）。

## 反模式（避免）

- ❌ 直接传文件路径给主 API（如 `Database("db/market.iosql")`）→ 必须用 `sqlite://` 前缀
- ❌ `from iosql import DurationPartition` → 顶层已不导出分区类，用 `Partition.duration(...)` 工厂或 `iosql.partition.duration` 内部路径
- ❌ 对已存在表重复声明不同契约 → 用 `db.table("name")` 获取引用而非重新声明
- ❌ 在写入前未声明 `pk` → `write()` 必须有主键
- ❌ 未声明 `symbol_column` 的普通表调用 `query(symbols=)`/`latest(symbols=)` → 报 BadArgument；先 `symbol_column="symbol"` 显式声明或改用 `by=`/`where=`
- ❌ 依赖旧版 `select()` 默认返回 tuple → 现默认 dict 行，需要 tuple 显式 `to_dict=False`
- ❌ 用 `delete(None)` → 已废弃，改用 `truncate(confirm=True)`
- ❌ 假设 `repartition_to()` 会修改原库 → 它只写新库
- ❌ 拼接原始 SQL 做 where → 用 dict/list/`Case`/文本表达式
- ❌ 忽略 `WriteResult` 返回值 → 检查 `rows_seen` vs `rows_written`
- ❌ 吞掉 write_buffer 异常退出时的 RuntimeWarning → 提示的是未 flush 行数，意味着数据未落库
- ❌ 忽略测试数据隔离 → 每个测试用独立 `tmp_path`
- ❌ 用 `insert_all()` 替代 `write()` → `insert_all` 是低层 API，不自动使用表契约
- ❌ mysqlite `insert_all(commit=False)` → 已显式拒绝；需要事务用 `db.transaction()`

## 引用资料

所有资料均包含在 skills 目录内，同步分发：

- **代码模板**：[usage-patterns.md](references/usage-patterns.md) —— 涵盖全部 Partition × Segment 组合、duration/calendar/range 分区、timeseries helper、缓冲写入、查询计划、布局诊断、重分区、晚到数据、金融场景（K线/orderbook/财报/新闻）、CLI 模式
- **可运行示例**：[examples/](examples/) —— 可直接运行的 Python 脚本，按学习顺序编号：
  - `01_quickstart_table.py`、`01_quickstart_timeseries.py` —— 入门：普通 Table vs timeseries helper
  - `02_calendar_events_table.py`、`02_calendar_events.py` —— 日历分区处理日期字符串
  - `03_buffered_ingest_and_query_plan.py` —— 批量缓冲写入 + 查询计划
  - `04_layout_report_and_repartition.py` —— 布局诊断 + 重分区
  - `05_partition_segments_smoke.py` —— 逻辑分区 + 物理 segment
  - `06_row_segment_late_data.py` —— 纯 row segment + 晚到数据
  - `07_partition_string_repartition_cli.py` —— 字符串 partition + CLI 重分区
  - `08_doctor_time_first_suggestions.py` —— doctor 对历史 `partition_key` 表优先推荐时间分区
  - `09_existing_database_append.py` —— 自包含地模拟已有库并按持久化契约继续写入
  - `10_existing_database_repartition.py` —— 自包含地模拟已有源库并重分区到新目标
  - `11_sqlite_to_postgresql_migration.py` —— SQLite 源库 dry-run、迁移到 PostgreSQL、row-count 校验和目标侧查询；未设置 `POSTGRES_DSN` 时只执行 dry-run
  - `12_sqlite_postgresql_roundtrip.py` —— SQLite -> PostgreSQL -> SQLite 双向转换；未设置 `POSTGRES_DSN` 时只执行 dry-run
  - `mysqlite/` —— 低层 mysqlite 工具：schema-on-write、批量写入与冲突处理、连接池清理（非主 API 入口）
  - `mysqlite-scenarios/` —— mysqlite 场景教学：CRUD/事务、加删列与缓存边界、查询变体、RENAME 与表信息、索引操作与 autoindex 坑、在线备份
  - `finance/` —— 金融场景：K线、orderbook、财报、新闻
- **错误排查**：[troubleshooting.md](references/troubleshooting.md)
- **写入性能**：[write-performance.md](references/write-performance.md) —— 2500 万行日线、orderbook、ext4 HDD benchmark、批次/分区/segment/PRAGMA 最佳实践
- **API 签名**：[api-reference.md](references/api-reference.md)
