"""用 row segment 保存并查询晚到数据。

运行：在本文件所在目录执行 python 06_row_segment_late_data.py。
准备：已安装 iosql 及其默认依赖。
预期：输出 segment 统计、查询结果和裁剪信息。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "06_row_segment_late_data.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

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
