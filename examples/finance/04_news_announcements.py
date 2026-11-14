"""保存新闻和公告并按时间及类型查询。

运行：在本文件所在目录执行 python 04_news_announcements.py。
准备：已安装 iosql 及其默认依赖。
预期：输出事件分区、扫描计划和当日公告。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Segment


EXAMPLE_DIR = Path(__file__).resolve().parent
DB_PATH = EXAMPLE_DIR / "runtime" / uuid4().hex / "04_news.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        events = db.timeseries(
            "news_events",
            time="event_ts",
            pk=["symbol", "event_ts", "event_id"],
            columns={
                "symbol": "text",
                "event_ts": "integer",
                "event_id": "text",
                "event_type": "text",
                "source": "text",
                "title": "text",
            },
            partition_every="1day",
            segment=Segment.rows(target_rows=300_000),
        )

        day0 = 1772323200000
        events.write([
            {
                "symbol": "AAPL",
                "event_ts": day0 + 9 * 3_600_000,
                "event_id": "n1",
                "event_type": "news",
                "source": "wire",
                "title": "supplier capacity update",
            },
            {
                "symbol": "AAPL",
                "event_ts": day0 + 10 * 3_600_000,
                "event_id": "a1",
                "event_type": "announcement",
                "source": "ir",
                "title": "quarterly results date",
            },
            {
                "symbol": "AAPL",
                "event_ts": day0 + 12 * 3_600_000,
                "event_id": "n2",
                "event_type": "news",
                "source": "wire",
                "title": "cloud region expansion",
            },
        ])

        query = events.query(
            symbols="AAPL",
            date="2026-03-01",
            where={"field": "event_type", "op": "==", "value": "announcement"},
            order_by="event_ts desc",
        )

        print("事件分区:", events.table_parts())
        print("扫描分区:", query.explain().scanned_partitions)
        print("AAPL 当日公告:", query.to_list())


if __name__ == "__main__":
    run()
