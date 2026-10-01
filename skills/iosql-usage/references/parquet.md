# Parquet 后端使用指南

本文说明何时选择 Parquet、怎样通过 iosql 读写和维护目录库，以及它与 SQLite 的行为差异。
完整 API 参数见 [API 参考](api-reference.md)，命令边界见 [CLI 参考](cli.md)。

## 适用场景与安装

Parquet 适合批量归档、历史数据回灌、按列投影分析和跨分区有序读取。追加、修改和删除
会重写受影响的物理文件；高频逐行改写前应先评估重写成本。

安装可选依赖：

~~~bash
python -m pip install "iosql[parquet]"
~~~

在仓库开发环境中使用：

~~~bash
python -m pip install -e ".[parquet]"
~~~

## 打开、建表与查询

Parquet 目录库使用 `parquet://` URI。建表、查询和 SQLite 后端使用相同的契约 API：

~~~python
from iosql import Database, Segment

with Database("parquet://db/archive.iosql") as db:
    bars = db.timeseries(
        "bars",
        time="ts",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "close": "float"},
        partition_every="1day",
        segment=Segment.rows(max_rows=500_000),
    )
    bars.write([{"symbol": "AAPL", "ts": 1735689600000, "close": 187.4}])
    rows = bars.query(symbols="AAPL", order_by="ts").to_list()
~~~

重开已有表时用 `db.table("bars")` 读取其持久化契约，避免重复声明。Parquet 支持
`duration`、`calendar`、`range` 分区和 `Segment.rows`；列类型使用 iosql 的
`integer`、`float`、`text`、`blob` 及常见别名。增加列后，旧文件里不存在的列会读为
`NULL`。

支持常用 `write()`、`buffered_writer()`、`select()`、`query()`、`latest()`、
`iter_select_batches()` / `iter_batches()`、`layout_report()`、`validate()`、
`compact()`、`repartition_to()` 和表级 `rename()`。先用 `explain()` 或
`explain_select()` 检查分区与 segment 裁剪；大结果集使用流式接口，不要先物化整表。

## 存储布局与写入语义

每张表的 `meta.sqlite3` 保存契约和 manifest，业务数据保存在
`<table>/data/*.parquet`，文件使用 ZSTD 压缩。iosql 只识别自己管理的目录库，不会
自动把任意 Parquet 文件注册成表。

`append`、`upsert`、`replace`、`ignore` 和条件删除会重写命中的文件。将多行合并为批次，
或用 `buffered_writer(max_rows=...)` flush，避免逐行写入；用 `Segment.rows(max_rows=...)`
限制单个物理文件大小。与其他后端一致，`Segment.rows` 表不支持 `upsert`/`replace`。
带逻辑分区的 `upsert`/`replace` 要求分区列也属于主键；`append`/`ignore` 的唯一性
由各物理文件约束，跨文件唯一性需由调用方保证。

文件替换以单个 Parquet 文件为原子单位；多文件写入和 manifest 更新没有统一事务。
跨文件读取没有统一快照。需要一致迁移时，先暂停源库写入。

## 流式读取与并发

流式查询打开期间会保留数据文件的读取租约。其他线程写同一张表时会等待读取结束；
持有读取流的线程直接写同表会报错。先耗尽迭代器，或显式关闭流，再写入、整理或清理。
全局 `order_by` 的流式归并受 `max_ordered_read_connections` 和
`max_process_connections` 预算约束。

## 与 SQLite 的能力差异

Parquet 暂不支持：

- SQLite `PRAGMA`。
- 持久化 SQL 索引（`create_index` / `drop_index`）。
- `commit=False`。
- `aggregate_by_bucket()`。
- 整个数据库目录的 `Database.rename()`。

表级重命名与 Python 维护接口可用。Web 工作区仍按 SQLite 目录库发现数据。

## SQLite 与 Parquet 迁移

使用 `Database.migrate_to()` 在 SQLite 与 Parquet 之间流式复制表契约和行数据。
可用 `dry_run=True` 预览源行数和批次数，用 `verify="row-count"` 检查迁移后的行数：

~~~python
from iosql import Database

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
    assert result.verified

with Database("parquet://db/archive.iosql") as source:
    restored = source.migrate_to(
        "sqlite://db/restored.iosql",
        verify="row-count",
        batch_size=50_000,
    )
~~~

`tables` 可选全库、表名列表或 `{源表: 目标表}` 映射。迁移目标不能与源目录相同或位于
源目录内部；行数校验应使用干净的目标表。迁移前确认源表有持久化契约，并在迁移窗口暂停
写入。

CLI 只为 `migrate` 提供 Parquet URI 支持：

~~~bash
iosql migrate -s sqlite://db/market.iosql \
  --target parquet://db/archive.iosql --dry-run --verify row-count

iosql migrate -s parquet://db/archive.iosql \
  --target sqlite://db/restored.iosql --verify row-count
~~~

其他 CLI 运维命令与 Web 工作区仍使用 SQLite 目录库。完整、可重复运行的流程见
[Parquet 示例](../examples/14_parquet_backend.py)。

## 常见问题

- `提示缺少 DuckDB`：安装 `iosql[parquet]`，本地开发安装可用
  `python -m pip install -e ".[parquet]"`。
- `已有普通 Parquet 文件找不到表`：iosql 不会自动接管任意文件；需先通过 iosql API
  写入其管理的目录库，或迁移一份已有 iosql 表。
- `同线程写入时报读取流错误`：先耗尽或关闭 `iter_rows()` / `iter_batches()` 流。
- `写入时间随文件变大而增加`：Parquet 修改需要重写受影响文件；增大写入批次并检查
  `Segment.rows` 大小是否适合该表。
