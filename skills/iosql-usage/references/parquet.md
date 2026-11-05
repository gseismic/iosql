# Parquet 后端使用指南

本文说明何时选择 Parquet、怎样通过 iosql 读写和维护目录库，以及它与 SQLite 的行为差异。
完整 API 参数见 [API 参考](api-reference.md)，命令边界见 [CLI 参考](cli.md)。

## 适用场景与安装

Parquet 适合批量归档、历史数据回灌、按列投影分析和跨分区有序读取。append/ignore
通过不可变数据片追加；修改和删除重写受影响的物理集合。

Parquet 后端依赖 DuckDB，已包含在 iosql 默认依赖中。普通安装即可使用：

~~~bash
python -m pip install iosql
~~~

在仓库开发环境中使用：

~~~bash
python -m pip install -e .
~~~

## 打开、建表与查询

创建 Parquet 目录库可使用 `parquet://` 或 `iosql+parquet://` URI，也可调用
`Database.create(path, engine="parquet")`；已有库可直接传本地路径自动识别。
建表、查询和 SQLite 后端使用相同的契约 API：

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

新库的 `__iosql_database__.json` 标识存储类型，每张表的 `meta.sqlite3` 保存契约和 manifest，业务数据保存在
`<table>/data/*.parquet` 入口及其专属隐藏数据片目录，文件使用 ZSTD 压缩。
追加后由 `<入口>.manifest.json` 列出可见文件；应通过 API、备份或迁移复制完整集合，
不要只复制入口文件。iosql 只识别自己管理的目录库，不会
自动把任意 Parquet 文件注册成表。

`append`/`ignore` 仅写出本批新增行，历史主键检查可能扫描旧键列；`upsert`/`replace`
及条件删除仍重写命中的集合。将多行合并为批次，或用 `buffered_writer(max_rows=...)`
flush，限制数据片数量；用 `Segment.rows(max_rows=...)`
限制单个物理文件大小。与其他后端一致，`Segment.rows` 表不支持 `upsert`/`replace`。
带逻辑分区的 `upsert`/`replace` 要求分区列也属于主键；`append`/`ignore` 的唯一性
由各物理文件约束，跨文件唯一性需由调用方保证。

单个物理集合在数据片落盘后原子切换清单，发布前失败不改变旧可见数据。
旧数据片在发布后清理，清理失败不撤销成功发布；多物理集合写入仍没有统一事务。
普通跨文件读取没有统一快照。`migrate_to()` 全程持源/目标独占租约，
协作 API 在此期间报 `DatabaseBusy`；迁移前先关闭读取流、flush 缓冲并退出缓冲写入上下文。

## 流式读取与并发

流式查询打开期间会保留数据文件的读取租约。其他线程写同一张表时会等待读取结束；
持有读取流的线程直接写同表会报错。先耗尽迭代器，或显式关闭流，再写入、整理或清理。
全局 `order_by` 的流式归并受 `max_ordered_read_connections` 和
`max_process_connections` 预算约束。
DuckDB 连接也计入进程预算；有序扫描在预留配额前取得元数据，预算无法满足时快速报错，
连接等待、执行与批次消费共同受 `timeout_ms` 截止时间约束。每连接内存上限 256MB，
排序等操作可使用临时磁盘；compact 可合并数据片并用外部排序限制 Python 内存。

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

`tables` 可选全库、表名列表或 `{源表: 目标表}` 映射，`specs` 按源名调整目标分区/segment。
本地源与目标不能相同或互相包含；行数校验要求选中目标表不存在或为空。
迁移前确认源表有完整契约，释放读取流和缓冲上下文，并预留暂存目标视图的空间。
本地目标默认在目标文件系统暂存；如需自行选择位置，可用 `temp_dir` / `--temp-dir` 指定目标库之外的工作区。
dry-run 只读源、不连接目标，不验证目标权限、已有契约或空表条件；可能创建源协调锁文件。

迁移先转换、校验，再统一发布；Parquet 表在发布后直接位于目标库根目录，与普通写入一致。
旧版 Parquet 代目录库可作为源读取，但不能直接作为平坦发布的迁移目标；可迁移到新的目标。
成功时检查 `result.published` 与 `result.verified`；
`rows_copied` 可包含 ignore 跳过的行，实际写入量看 `rows_written`。失败时依据异常中的
`operation_id`、`partial_result` 和 `recovery_path` 查明状态，再决定重试；
详见[迁移错误排查](troubleshooting.md#统一迁移与协作锁)。

CLI 可自动识别已有 Parquet 源库，也可显式传 `parquet://`：

~~~bash
iosql migrate -s sqlite://db/market.iosql \
  --target parquet://db/archive.iosql --dry-run --verify row-count

iosql migrate -s parquet://db/archive.iosql \
  --target sqlite://db/restored.iosql --verify row-count
~~~

`iosql web -s db/archive.iosql` 支持数据浏览和图表；新迁移目标可传
`--target-engine parquet`。`doctor`、`index`、`repartition`、`backup`、`restore` 和
`rename-database` 仍限 SQLite。SQLite → Parquet 的单向迁移示例见
[16_sqlite_to_parquet.py](../examples/16_sqlite_to_parquet.py)；Parquet 后端和双向迁移的完整示例见
[14_parquet_backend.py](../examples/14_parquet_backend.py)。

## 常见问题

- `提示缺少 DuckDB`：重新安装 iosql 及其默认依赖；本地开发安装可用
  `python -m pip install -e .`，普通环境可用 `python -m pip install --upgrade iosql`。
- `已有普通 Parquet 文件找不到表`：iosql 不会自动接管任意文件；需先通过 iosql API
  写入其管理的目录库，或迁移一份已有 iosql 表。
- `同线程写入时报读取流错误`：先耗尽或关闭 `iter_rows()` / `iter_batches()` 流。
- `写入时间随文件变大而增加`：追加仍有主键检查成本，修改需要重写命中集合；
  合并小批、检查 `Segment.rows` 大小，并用 compact/repartition 整理文件布局。
