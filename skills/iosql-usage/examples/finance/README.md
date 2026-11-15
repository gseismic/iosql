# 金融场景示例

这些示例面向常见金融数据写入和按时间段查询。每个脚本可以单独复制运行，每次在脚本所在目录的 `runtime/` 下创建独立目录并保留结果。

下列顺序按行情、盘口和低频事件的日常使用场景定性估计，并非使用统计；保留历史文件编号。

夜盘、期货和跨时区行情不要把 `date=` 当作交易日过滤；应在建表时声明 `session_column="trading_date"`，再用 `query(session=...)` 查询交易日/session。

| 文件 | 场景 | 重点 |
| --- | --- | --- |
| [01_kline_bars.py](01_kline_bars.py) | K 线 OHLCV | 毫秒时间戳、日分区、按标的和时间段查询。 |
| [02_orderbook_snapshots.py](02_orderbook_snapshots.py) | orderbook 快照 | 高频批量写入、复合主键、小时分区。 |
| [03_financial_reports.py](03_financial_reports.py) | 财报指标 | 日期字段、月度日历分区、日期范围裁剪。 |
| [04_news_announcements.py](04_news_announcements.py) | 新闻/公告事件 | 事件时间、类型过滤、查询计划。 |

在仓库根目录安装后选择一个脚本运行，不需要设置模块搜索路径：

```bash
python -m pip install -e .
python examples/finance/01_kline_bars.py
python examples/finance/02_orderbook_snapshots.py
python examples/finance/03_financial_reports.py
python examples/finance/04_news_announcements.py
```
