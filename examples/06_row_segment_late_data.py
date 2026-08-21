"""高级：仅按行数滚动物理 segment，并处理晚到数据。"""

from pathlib import Path
import shutil

from iosql import Database, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / "06_row_segment_late_data.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    with Database(DB_URI) as db:
        events = db.table(
            "events",
            pk=["event_id"],
            columns={"event_id": "integer", "seq": "integer", "kind": "text"},
            segment=Segment.rows(max_rows=3),
            order_key="seq",
        )

        events.write([
            {"event_id": index, "seq": index, "kind": "base"}
            for index in range(6)
        ])
        events.write([
            {"event_id": 99, "seq": 1, "kind": "late"},
        ])

        where = {"field": "seq", "op": "between", "value": [1, 1]}
        rows = events.select(where=where, order_by="event_id", to_dict=True)
        plan = events.explain_select(where=where, order_by="seq")

        print("逻辑分区:", events.table_parts())
        print("segments:", [
            {
                "part_key": item["part_key"],
                "row_count": item["row_count"],
                "seq_stats": item["stats"]["seq"],
            }
            for item in events.segments()
        ])
        print("查询结果:", rows)
        print("扫描 segment:", [item["part_key"] for item in plan.scanned_segments])
        print("跳过 segment:", plan.segment_skip_reasons)
        print("需要全局排序:", plan.requires_global_sort)


if __name__ == "__main__":
    run()
