# iosql 示例

本目录只保留主 API 的少量代表性示例。编号越小越适合入门，后面的示例覆盖批量写入、查询计划和布局维护。主线示例使用普通 `Table` + `Partition` + `Segment`；`timeseries()` 文件保留为 helper 版本。

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
PYTHONPATH=. python examples/finance/01_kline_bars.py
```

## 运行时数据

示例会写入 `examples/runtime/`，该目录不需要提交。每个脚本启动时会清理自己的示例库，便于重复运行。

## 低层 mysqlite 示例

`examples/mysqlite/` 保留少量 `iosql.utils.mysqlite` 示例，用于直接操作单个 SQLite 文件、schema-on-write、批量写入和连接池清理。新项目优先使用本目录上方的 `Database.table(...)` 主 API 示例。
