"""入门：用普通 Table + calendar 分区保存日期字符串事件表。"""

from pathlib import Path
import shutil

from iosql import Database, Partition


DB_PATH = Path(__file__).resolve().parent / "runtime" / "02_calendar_table.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    events = db.table(
        "events",
        pk=["event_date", "name"],
        partition=Partition.calendar(
            "event_date",
            every="1day",
            timezone="Asia/Shanghai",
        ),
        columns={"event_date": "text", "name": "text", "city": "text"},
    )
    events.insert_all([
        {"event_date": "2026-01-05", "name": "open", "city": "Shanghai"},
        {"event_date": "2026-01-06", "name": "review", "city": "Beijing"},
        {"event_date": "2026-01-06", "name": "close", "city": "Shanghai"},
    ])

    rows = events.select(
        "*",
        where={"field": "event_date", "op": "==", "value": "2026-01-06"},
        order_by="name",
        to_dict=True,
    )
    plan = events.explain_select(
        where={"field": "event_date", "op": "==", "value": "2026-01-06"},
    )

    print("日历分区:", events.table_parts())
    print("扫描分区:", plan.scanned_partitions)
    print("2026-01-06 的事件:", rows)
    db.close()


if __name__ == "__main__":
    run()
