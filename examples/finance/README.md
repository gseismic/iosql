# 金融场景示例

这些示例面向常见金融数据写入和按时间段查询。每个脚本都会清理自己的运行时目录，可以重复运行。

夜盘、期货和跨时区行情不要把 `date=` 当作交易日过滤；应在建表时声明 `session_column="trading_date"`，再用 `query(session=...)` 查询交易日/session。

| 文件 | 场景 | 重点 |
| --- | --- | --- |
| `01_kline_bars.py` | K 线 OHLCV | 毫秒时间戳、日分区、按标的和时间段查询。 |
| `02_orderbook_snapshots.py` | orderbook 快照 | 高频批量写入、复合主键、小时分区。 |
| `03_financial_reports.py` | 财报指标 | 日期字段、月度日历分区、日期范围裁剪。 |
| `04_news_announcements.py` | 新闻/公告事件 | 事件时间、类型过滤、查询计划。 |

运行：

```bash
PYTHONPATH=. python examples/finance/01_kline_bars.py
PYTHONPATH=. python examples/finance/02_orderbook_snapshots.py
PYTHONPATH=. python examples/finance/03_financial_reports.py
PYTHONPATH=. python examples/finance/04_news_announcements.py
```
