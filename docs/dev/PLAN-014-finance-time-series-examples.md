# PLAN-014 金融时序场景支持与示例

## 背景

目标场景包括金融财报、K 线、orderbook、新闻事件和公告。这些数据都有明确的时间字段，并且常见访问方式是按标的和时间段快速查询。

当前实现已经具备时序表契约、按时间分区、`query(start/end/date)`、`query().explain()`、全局排序和布局诊断能力。主要缺口在于：

- `Database.timeseries()` 只暴露毫秒时间戳入口，财报/公告这类日期字段需要手写 `TableSpec`。
- 日期/日历分区虽然能写入和查询，但查询裁剪主要依赖数值范围，日期字段的分区裁剪证据不足。
- 示例还没有覆盖典型金融数据形态。

## 支持性判断

| 场景 | 时间字段 | 推荐分区 | 主键建议 | 当前结论 |
| --- | --- | --- | --- | --- |
| K 线 | `ts` 毫秒时间戳 | `1day` / `1hour` duration | `symbol, timeframe, ts` | 已支持，需要示例说明。 |
| orderbook | `ts` 毫秒时间戳 | `1hour` / `1min` duration | `symbol, venue, ts, side, level` | 已支持，需要示例说明高频批量写入。 |
| 财报 | `report_date` 或 `published_date` 日期 | `1month` / `1day` calendar | `symbol, report_date, statement` | 需要让 `timeseries()` 直接支持 date/calendar。 |
| 新闻事件/公告 | `event_ts` 毫秒或 `published_date` 日期 | duration 或 calendar | `symbol, event_ts, event_id` | 已支持毫秒事件；日期公告需要 date/calendar 入口和裁剪优化。 |

## 实施目标

1. 扩展 `Database.timeseries()` / `TableSpec.timeseries()`：
   - 增加 `time_unit` 参数，默认 `ms`。
   - 增加 `partition_kind` 参数，默认 `duration`。
   - 支持用同一入口创建日期/日历分区表。
2. 优化日期/日历分区的时间段查询裁剪：
   - 对 `TimeColumn(unit="date")` 的 `=`, `>=`, `<=`, `<`, `>`, `between_and` 转换为分区毫秒范围进行裁剪。
   - 保持 SQL 查询条件仍使用原始日期字符串。
3. 新增金融示例：
   - K 线 OHLCV。
   - orderbook 快照。
   - 财报指标。
   - 新闻/公告事件。
4. 更新 README 和 examples README，加入金融场景入口说明。

## 验证

- 新增单元测试覆盖 date/calendar 裁剪和 `Database.timeseries(time_unit="date", partition_kind="calendar")`。
- 运行金融示例脚本。
- 运行 `python -m pytest tests/sqlite/test_timeseries_contract.py -q`。
- 运行 `python -m pytest tests -q`。
- 运行 `python -m compileall -q iosql examples`。
- 运行 `git diff --check`。

## 非目标

- 不实现专用金融数据模型或行情协议。
- 不实现 orderbook 增量撮合或盘口重建。
- 不引入外部金融数据源。
