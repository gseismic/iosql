"""入门：用日期字符串和日历分区保存事件表。"""

from pathlib import Path
import shutil

from iosql import Database


DB_PATH = Path(__file__).resolve().parent / "runtime" / "02_calendar.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    events = db.timeseries(
        "events",
        time="event_date",
        time_unit="date",
        partition_kind="calendar",
        partition_every="1day",
        timezone="Asia/Shanghai",
        primary_key=["event_date", "name"],
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
    db.close()


if __name__ == "__main__":
    run()
