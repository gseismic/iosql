# iosql 示例

本目录按“一个示例解决一类问题”组织主 API 示例。编号越小越适合入门，后面的示例覆盖批量写入、查询计划、流式读取、布局维护、已有库和迁移。主线示例使用普通 `Table` + `Partition` + `Segment`；`timeseries()` 文件保留为 helper 版本。

当前 `Segment.rows(...)` 示例均按 append-first 语义写入；在全局 pk index/tombstone 实现前，不演示跨 segment 的 `upsert`/`replace` 覆盖写。

## 示例索引

| 文件 | 难度 | 场景 |
| --- | --- | --- |
| `01_quickstart_table.py` | 入门 | 用 `Database.table(..., partition=Partition.duration(...))` 写入普通分区表，使用 `select(where=...)` 和 `explain_select(...)` 查询。 |
| `01_quickstart_timeseries.py` | 入门 | 用 `Database.timeseries()` 快捷创建同等时序 helper，保留 `query(start/end)` 便利查询。 |
| `02_calendar_events_table.py` | 入门 | 用 `Partition.calendar(...)` 保存日期字符串事件表，不需要核心 `time` 字段。 |
| `02_calendar_events.py` | 入门 | 用 `Database.timeseries()` 快捷创建日期字符串和日历分区事件表。 |
| `03_buffered_ingest_and_query_plan.py` | 进阶 | 使用 `buffered_writer()` 批量写入，并通过 `explain_select()` 查看分区和 segment 裁剪。 |
| `04_layout_report_and_repartition.py` | 高级 | 使用 `layout_report()` 诊断分区行数，并用 `repartition_to()` 生成新布局。 |
| `05_partition_segments_smoke.py` | 高级 | 验证逻辑分区 + 物理 row segment 的二级查询计划。 |
| `06_row_segment_late_data.py` | 高级 | 仅按行数滚动 segment，展示晚到数据、`__all__` 逻辑分区和 stats pruning。 |
| `07_partition_string_repartition_cli.py` | 高级 | 使用字符串 partition，并通过 CLI `repartition --partition --target` 生成新布局。 |
| `08_doctor_time_first_suggestions.py` | 高级 | 展示 `doctor` 如何对历史 `partition_key` 表优先推荐 `calendar(date,...)`。 |
| `09_existing_database_append.py` | 进阶 | 独立准备并关闭一个示例库，再按已有持久化契约重新打开并增量写入。 |
| `10_existing_database_repartition.py` | 高级 | 独立准备已有源库，重新打开后离线重分区到新的目标库。 |
| `11_sqlite_to_postgresql_migration.py` | 高级 | SQLite 源库 dry-run、迁移到 PostgreSQL、row-count 校验和目标侧查询。 |
| `12_sqlite_postgresql_roundtrip.py` | 高级 | SQLite -> PostgreSQL -> SQLite 双向转换和 row-count 校验。 |
| `13_streaming_ordered_query.py` | 进阶 | 对跨分区结果按 `open_time` 全局排序，并用 `query(...).iter_rows(...)` 惰性逐行读取。 |
| `14_parquet_backend.py` | 高级 | Parquet 分区存储、segment、流式排序、重开与 SQLite 双向迁移。 |
| `15_postgresql_timeseries_query.py` | 高级 | PG14+ 时间窗流式查询、latest/asof、原生计划、复合索引与字段更新。 |
| `16_sqlite_to_parquet.py` | 进阶 | 准备 SQLite 源表，dry-run 后迁移到 Parquet，并在目标端查询和校验。 |
| `17_safe_migration_and_paths.py` | 高级 | 演示大小写路径保留、安全迁移与备份、快照拒绝 junction、库锁拒绝符号链接。 |
| `18_save_as_compare_engines.py` | 进阶 | 用 `save_as()` 保留源库，校验 SQLite/Parquet 两份数据后比较相同查询的首次及预热耗时。 |

## 金融示例

金融场景示例放在 `examples/finance/`：

| 文件 | 场景 |
| --- | --- |
| `finance/01_kline_bars.py` | K 线 OHLCV 写入和时间窗口查询。 |
| `finance/02_orderbook_snapshots.py` | orderbook 快照批量写入和复合主键。 |
| `finance/03_financial_reports.py` | 财报日期字段、月度日历分区和日期范围查询。 |
| `finance/04_news_announcements.py` | 新闻/公告事件按时间和类型过滤。 |

## 运行

`14_parquet_backend.py` 演示 Parquet 分区文件、流式排序、重开和往返迁移；
`16_sqlite_to_parquet.py` 专门演示 SQLite → Parquet 单向迁移。
Parquet 后端随默认依赖安装，运行这些示例无需额外安装。

先在仓库根目录安装开发版本：

```bash
python -m pip install -e .
```

再运行任意示例：

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

`11_sqlite_to_postgresql_migration.py` 未设置 `POSTGRES_DSN` 时只执行 SQLite 源库准备和 dry-run；设置 `POSTGRES_DSN` 后会重建示例目标表并执行真实 PostgreSQL 迁移和行数校验。
`12_sqlite_postgresql_roundtrip.py` 未设置 `POSTGRES_DSN` 时只执行 dry-run；设置后会演示 `sqlite -> postgresql -> sqlite` 双向转换。

`17_safe_migration_and_paths.py` 在自己的临时演示目录中运行，结束后自动清理。Windows 上使用 `fsutil` 尝试启用大小写敏感目录，以 `mklink /J` 创建真实 junction；系统能力或权限不足时明确跳过对应场景。库锁符号链接需要 Windows 的相应权限。Linux/macOS 使用实际符号链接演示拒绝行为，输出会注明未验证 Windows 原机行为。快照部分使用与 CLI `iosql backup` 共用的内部维护入口。

`18_save_as_compare_engines.py` 用 20,000 行数据演示另存和查询计时；只重置自己的两个
演示路径，运行后保留源库及副本。复制和行数校验在查询计时前完成；两端使用相同条件、
投影、排序和结果消费方式，先确认结果一致，再交替顺序重复查询并报告中位耗时。
首次查询不代表系统冷缓存。需要比较实际业务性能时，用自己的数据规模、索引和查询负载，
大规模写入/读取基准可参考 `benchmarks/benchmark_wide_backends.py`。

## 运行时数据

示例会写入 `examples/runtime/`，该目录不需要提交。每个脚本只清理自己拥有的默认
示例路径，便于独立重复运行。`09`、`10` 可通过环境变量指向真实已有库；此时不会
清理外部源库，重分区也只写新的目标 URI。

## 低层 mysqlite 示例

`examples/mysqlite/` 保留少量 `iosql.utils.mysqlite` 示例，用于直接操作单个 SQLite 文件、schema-on-write、批量写入和连接池清理。新项目优先使用本目录上方的 `Database.table(...)` 主 API 示例。

`examples/mysqlite-scenarios/` 按运维场景速查 mysqlite 完整能力面（增删改查、加列/删列、各类查询、表重命名与表信息、索引操作、在线备份），每个脚本独立可运行，详见该目录 README。

### PostgreSQL 本机查询示例

`15_postgresql_timeseries_query.py` 需要 PostgreSQL 14+ 与 `iosql[postgresql]`，使用随机表名并在结束时清理，可独立重复运行。

本机已安装 PostgreSQL 时，在示例目录执行：

```bash
bash start_postgresql_test.sh
p3 15_postgresql_timeseries_query.py
```

`p3` 是本机 `python3` 的别名；也可使用 `python3` 或 `uv run --extra postgresql`。启动脚本复用已安装程序，在 `runtime/postgresql-test/data` 保留独立测试库，监听 `127.0.0.1:55478`，使用无密码的本机测试身份 iosql。它不会修改系统数据库；重启电脑后重新运行启动脚本即可。其他安装位置可传 `POSTGRES_BIN=/path/to/bin`。

示例优先使用 `POSTGRES_DSN`，未设置时读取启动脚本生成的 `runtime/postgresql-test.dsn`。从仓库根目录运行 `p3 examples/15_postgresql_timeseries_query.py` 也会读取同一文件。其他 PG 示例和基准可先执行：

```bash
source runtime/postgresql-test.env
```

使用已有测试数据库时直接设置 `POSTGRES_DSN` 即可，例如 `POSTGRES_DSN='postgresql://user:password@host:5432/testdb' p3 15_postgresql_timeseries_query.py`；不需要本机启动脚本。

停止本机测试库：

```bash
/usr/lib/postgresql/14/bin/pg_ctl -D runtime/postgresql-test/data -m fast -w stop
```

使用其他版本或安装位置时替换为对应的 pg_ctl 路径。
