"""入门：用日期字符串和日历分区保存事件表。"""

from pathlib import Path
import shutil

from iosql import Database, Partition


DB_PATH = Path(__file__).resolve().parent / "runtime" / "02_calendar.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

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
