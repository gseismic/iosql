"""入门：直接用原始 Table + TableSpec 保存日期字符串事件表。"""

from pathlib import Path
import shutil

from iosql import Database, PartitionSpec, TableSpec, TimeColumn


DB_PATH = Path(__file__).resolve().parent / "runtime" / "02_calendar_table.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    spec = TableSpec(
        primary_key=["event_date", "name"],
        time=TimeColumn("event_date", unit="date", timezone="Asia/Shanghai"),
        partition=PartitionSpec.by_time(
            "event_date",
            every="1day",
            kind="calendar",
        ),
        columns={"event_date": "text", "name": "text", "city": "text"},
    )
    events = db.table("events", spec=spec)
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
