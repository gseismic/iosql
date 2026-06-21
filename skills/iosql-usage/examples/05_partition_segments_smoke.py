"""高级：验证逻辑分区 + 物理 row segment 布局。"""

from pathlib import Path
import shutil

from iosql import Database, Partition, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / "05_partition_segments.iosql"
DB_URI = f"sqlite://{DB_PATH}"
TOTAL_DAYS = 6
ROWS_PER_DAY = 48
SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA"]


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


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
    reset_database()

    db = Database(DB_URI)
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
    result = ticks.insert_all(rows)
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
        {"field": "ts", "op": "between_and", "value": [start, end]},
    ]
    query_rows = ticks.select("*", where=where, order_by="ts", to_dict=True)
    plan = ticks.explain_select(where=where, order_by="ts")

    print("写入行数:", result.rows_seen)
    print("逻辑分区:", ticks.table_parts())
    print("物理 segment 数:", len(segments))
    print("首个 segment 概览:", first_segment)
    print("布局建议:", report.recommendation)
    print("实际扫描分区:", plan.scanned_partitions)
    print("扫描 segment:", [item["part_key"] for item in plan.scanned_segments])
    print("AAPL 第 3 天窗口行数:", len(query_rows))
    db.close()


if __name__ == "__main__":
    run()
