"""用自然日期和日历分区保存并查询事件。

运行：在本文件所在目录执行 python 02_calendar_events.py。
准备：已安装 iosql 及其默认依赖。
预期：输出日历分区和 2026-01-06 的事件。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "02_calendar.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        # calendar 场景直接传 partition=Partition.calendar(...)，
        # 不再使用已废弃的 time_unit/partition_kind 参数
        events = db.timeseries(
            "events",
            time="event_date",
            partition=Partition.calendar(
                "event_date",
                every="1day",
                timezone="Asia/Shanghai",
            ),
            pk=["event_date", "name"],
            columns={"event_date": "text", "name": "text", "city": "text"},
        )
        events.write([
            {"event_date": "2026-01-05", "name": "open", "city": "Shanghai"},
            {"event_date": "2026-01-06", "name": "review", "city": "Beijing"},
            {"event_date": "2026-01-06", "name": "close", "city": "Shanghai"},
        ])

        rows = events.query(date="2026-01-06", order_by="name").to_list()

        print("日历分区:", events.table_parts())
        print("2026-01-06 的事件:", rows)


if __name__ == "__main__":
    run()
