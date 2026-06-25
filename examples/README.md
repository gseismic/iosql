# iosql 示例

本目录只保留主 API 的少量代表性示例。编号越小越适合入门，后面的示例覆盖批量写入、查询计划和布局维护。主线示例使用普通 `Table` + `Partition` + `Segment`；`timeseries()` 文件保留为 helper 版本。

当前 `Segment.rows(...)` 示例均按 append-first 语义写入；在全局 pk index/tombstone 实现前，不演示跨 segment 的 `upsert`/`replace` 覆盖写。

## 示例索引

| 文件 | 难度 | 场景 |
| --- | --- | --- |
| `01_quickstart_table.py` | 入门 | 用 `Database.table(..., partition=Partition.duration(...))` 写入普通分区表，使用 `select(where=...)` 和 `explain_select(...)` 查询。 |
| `01_quickstart_timeseries.py` | 入门 | 用 `Database.timeseries()` 快捷创建同等时序 helper，保留 `query(start/end)` 便利查询。 |
| `02_calendar_events_table.py` | 入门 | 用 `Partition.calendar(...)` 保存日期字符串事件表，不需要核心 `time` 字段。 |
| `02_calendar_events.py` | 入门 | 用 `Database.timeseries()` 快捷创建日期字符串和日历分区事件表。 |
| `03_buffered_ingest_and_query_plan.py` | 进阶 | 使用 `write_buffer()` 批量写入，并通过 `explain_select()` 查看分区和 segment 裁剪。 |
| `04_layout_report_and_repartition.py` | 高级 | 使用 `layout_report()` 诊断分区行数，并用 `repartition_to()` 生成新布局。 |
| `05_partition_segments_smoke.py` | 高级 | 验证逻辑分区 + 物理 row segment 的二级查询计划。 |
| `06_row_segment_late_data.py` | 高级 | 仅按行数滚动 segment，展示晚到数据、`__all__` 逻辑分区和 stats pruning。 |
| `07_partition_string_repartition_cli.py` | 高级 | 使用字符串 partition，并通过 CLI `repartition --partition --target` 生成新布局。 |
| `08_doctor_time_first_suggestions.py` | 高级 | 展示 `doctor` 如何对历史 `partition_key` 表优先推荐 `calendar(date,...)`。 |
| `09_existing_database_append.py` | 进阶 | 独立准备并关闭一个示例库，再按已有持久化契约重新打开并增量写入。 |
| `10_existing_database_repartition.py` | 高级 | 独立准备已有源库，重新打开后离线重分区到新的目标库。 |
| `11_sqlite_to_postgresql_migration.py` | 高级 | SQLite 源库 dry-run、迁移到 PostgreSQL、row-count 校验和目标侧查询。 |
| `12_sqlite_postgresql_roundtrip.py` | 高级 | SQLite -> PostgreSQL -> SQLite 双向转换和 row-count 校验。 |

## 金融示例

金融场景示例放在 `examples/finance/`：

| 文件 | 场景 |
| --- | --- |
| `finance/01_kline_bars.py` | K 线 OHLCV 写入和时间窗口查询。 |
| `finance/02_orderbook_snapshots.py` | orderbook 快照批量写入和复合主键。 |
| `finance/03_financial_reports.py` | 财报日期字段、月度日历分区和日期范围查询。 |
| `finance/04_news_announcements.py` | 新闻/公告事件按时间和类型过滤。 |

## 运行

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
PYTHONPATH=. python examples/finance/01_kline_bars.py
```

`11_sqlite_to_postgresql_migration.py` 未设置 `POSTGRES_DSN` 时只执行 SQLite 源库准备和 dry-run；设置 `POSTGRES_DSN` 后会重建示例目标表并执行真实 PostgreSQL 迁移和行数校验。
`12_sqlite_postgresql_roundtrip.py` 未设置 `POSTGRES_DSN` 时只执行 dry-run；设置后会演示 `sqlite -> postgresql -> sqlite` 双向转换。

## 运行时数据

示例会写入 `examples/runtime/`，该目录不需要提交。每个脚本只清理自己拥有的默认
示例路径，便于独立重复运行。`09`、`10` 可通过环境变量指向真实已有库；此时不会
清理外部源库，重分区也只写新的目标 URI。

## 低层 mysqlite 示例

`examples/mysqlite/` 保留少量 `iosql.utils.mysqlite` 示例，用于直接操作单个 SQLite 文件、schema-on-write、批量写入和连接池清理。新项目优先使用本目录上方的 `Database.table(...)` 主 API 示例。
