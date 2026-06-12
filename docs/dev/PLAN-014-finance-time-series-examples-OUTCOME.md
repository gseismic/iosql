# PLAN-014 实施结果

## 完成内容

1. 金融场景支持性分析
   - 明确 K 线、orderbook、财报、新闻/公告的时间字段、推荐分区和主键设计。
   - 结论：毫秒时间戳场景已适合 `duration` 分区；财报/公告日期场景需要更方便的 date/calendar 入口和日期分区裁剪。

2. API 优化
   - `Database.timeseries()` 新增 `time_unit` 和 `partition_kind` 参数。
   - `TableSpec.timeseries()` 同步新增 `time_unit` 和 `partition_kind` 参数。
   - 新参数放在参数列表末尾，保持已有位置参数调用兼容。
   - 现在可直接使用：
     `db.timeseries(..., time_unit="date", partition_kind="calendar")`

3. 查询裁剪优化
   - 对 `TimeColumn(unit="date")` 的时间条件进行分区范围转换。
   - 支持 `=`, `>=`, `<=`, `<`, `>`, `between_and` 的日期分区裁剪。
   - SQL 查询条件仍保留日期字符串；仅分区规划使用毫秒范围比较。

4. 金融示例
   - 新增 `examples/finance/README.md`。
   - 新增 `examples/finance/01_kline_bars.py`：K 线 OHLCV。
   - 新增 `examples/finance/02_orderbook_snapshots.py`：orderbook 快照。
   - 新增 `examples/finance/03_financial_reports.py`：财报日期/日历分区。
   - 新增 `examples/finance/04_news_announcements.py`：新闻/公告事件。
   - 更新根 `README.md` 和 `examples/README.md`，加入金融场景说明和示例入口。

## Review 修复

- 金融 orderbook 示例第一次运行发现布局参数不合法：`target_rows_per_file=200_000` 小于默认 `min_rows_per_file=300_000`。已显式设置 `min_rows_per_file=100_000` 和 `max_rows_per_file=300_000`。
- K 线示例第一次输出出现浮点显示噪音，已用 `round(..., 2)` 固定展示。
- 接口 review 发现新增参数若插入 `schema_policy` 前会破坏已有位置参数兼容，已将 `time_unit` 和 `partition_kind` 移到参数列表末尾。

## 验证

已运行：

```bash
PYTHONPATH=. python examples/finance/01_kline_bars.py
PYTHONPATH=. python examples/finance/02_orderbook_snapshots.py
PYTHONPATH=. python examples/finance/03_financial_reports.py
PYTHONPATH=. python examples/finance/04_news_announcements.py
PYTHONPATH=. python examples/01_quickstart_timeseries.py
PYTHONPATH=. python examples/02_calendar_events.py
PYTHONPATH=. python examples/03_buffered_ingest_and_query_plan.py
PYTHONPATH=. python examples/04_layout_report_and_repartition.py
python -m pytest tests/sqlite/test_timeseries_contract.py -q
python -m compileall -q iosql examples
git diff --check
python -m pytest tests -q
```

结果：

- 4 个金融示例均从空库成功运行，并输出扫描分区。
- 4 个通用示例仍可运行。
- `tests/sqlite/test_timeseries_contract.py` 通过：`10 passed`。
- 全量测试通过：`118 passed, 14 skipped`。
- `compileall` 和 `git diff --check` 均通过。
- pytest 仍有上层 `.pytest_cache` 不可写警告，不影响测试结果。
