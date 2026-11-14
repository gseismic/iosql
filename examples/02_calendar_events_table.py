"""按交易日查询跨自然日的夜盘事件。

运行：在本文件所在目录执行 python 02_calendar_events_table.py。
准备：已安装 iosql 及其默认依赖。
预期：输出属于同一交易日的夜盘和日盘两条事件。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition


def run():
    path = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "events.iosql"
    path.parent.mkdir(parents=True, exist_ok=True)
    with Database(f"sqlite://{path}") as db:
        events = db.table(
            "events",
            pk=["event_date", "name"],
            columns={"event_date": "text", "trading_date": "text", "name": "text"},
            partition=Partition.calendar("event_date", every="1day", timezone="Asia/Shanghai"),
            session_column="trading_date",
        )
        events.write([
            {"event_date": "2026-01-05", "trading_date": "2026-01-06", "name": "night"},
            {"event_date": "2026-01-06", "trading_date": "2026-01-06", "name": "day"},
            {"event_date": "2026-01-06", "trading_date": "2026-01-07", "name": "next night"},
        ])

        # 自然日分区自动识别时间列；session 查询跨自然日筛选交易日。
        rows = events.query(session="2026-01-06", order_by="event_date").to_list()
        print("2026-01-06 交易日事件:", rows)


if __name__ == "__main__":
    run()
