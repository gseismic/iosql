# iosql 示例

本目录只保留主 API 的少量代表性示例。编号越小越适合入门，后面的示例覆盖批量写入、查询计划和布局维护。

## 示例索引

| 文件 | 难度 | 场景 |
| --- | --- | --- |
| `01_quickstart_timeseries.py` | 入门 | 创建毫秒时间戳时序表，写入数据，按 symbol 和时间窗口查询。 |
| `02_calendar_events.py` | 入门 | 使用日期字符串、时区和日历分区保存事件表。 |
| `03_buffered_ingest_and_query_plan.py` | 进阶 | 使用 `write_buffer()` 批量写入，并通过 `query().explain()` 查看分区裁剪。 |
| `04_layout_report_and_repartition.py` | 高级 | 使用 `layout_report()` 诊断分区行数，并用 `repartition_to()` 生成新布局。 |

## 运行

先在仓库根目录安装开发版本：

```bash
python -m pip install -e .
```

再运行任意示例：

```bash
python examples/01_quickstart_timeseries.py
python examples/02_calendar_events.py
python examples/03_buffered_ingest_and_query_plan.py
python examples/04_layout_report_and_repartition.py
```

## 运行时数据

示例会写入 `examples/runtime/`，该目录不需要提交。每个脚本启动时会清理自己的示例库，便于重复运行。
