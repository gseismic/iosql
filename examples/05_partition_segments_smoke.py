"""观察逻辑分区、物理 segment 和查询裁剪。

运行：在本文件所在目录执行 python 05_partition_segments_smoke.py。
准备：已安装 iosql 及其默认依赖。
预期：输出分区、segment 概览和窗口查询行数。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "05_partition_segments.iosql"
DB_URI = f"sqlite://{DB_PATH}"
TOTAL_DAYS = 6
ROWS_PER_DAY = 48
SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA"]


def build_rows():
    base_ts = 1735689600000
    rows = []
    for day in range(TOTAL_DAYS):
        day_start = base_ts + day * 86_400_000
        for index in range(ROWS_PER_DAY):
            symbol = SYMBOLS[index % len(SYMBOLS)]
            rows.append({
                "symbol": symbol,
                "ts": day_start + index * 1_800_000,
                "price": round(100 + day * 2 + index * 0.1, 2),
                "volume": 1_000 + day * ROWS_PER_DAY + index,
            })
    return rows


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        ticks = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={
                "symbol": "text",
                "ts": "integer",
                "price": "float",
                "volume": "integer",
            },
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=32, target_rows=24, min_rows=12),
            order_key="ts",
        )

        rows = build_rows()
        result = ticks.write(rows)
        report = ticks.layout_report()
        segments = ticks.segments()
        first_segment = {
            key: segments[0][key]
            for key in ["logical_part", "segment_id", "part_key", "row_count", "stats"]
        }

        start = rows[ROWS_PER_DAY * 2]["ts"]
        end = rows[ROWS_PER_DAY * 3 - 1]["ts"]
        where = [
            {"field": "symbol", "op": "==", "value": "AAPL"},
            {"field": "ts", "op": "between", "value": [start, end]},
        ]
        query_rows = ticks.select(where=where, order_by="ts", to_dict=True)
        plan = ticks.explain_select(where=where, order_by="ts")

        print("写入行数:", result.rows_seen)
        print("逻辑分区:", ticks.table_parts())
        print("物理 segment 数:", len(segments))
        print("首个 segment 概览:", first_segment)
        print("布局建议:", report.recommendation)
        print("实际扫描分区:", plan.scanned_partitions)
        print("扫描 segment:", [item["part_key"] for item in plan.scanned_segments])
        print("AAPL 第 3 天窗口行数:", len(query_rows))


if __name__ == "__main__":
    run()
