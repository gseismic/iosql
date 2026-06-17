"""高级：用当前时序 API 验证多分区和物理 segment 布局。"""

from pathlib import Path
import shutil

from iosql import Database


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
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={
            "symbol": "text",
            "ts": "integer",
            "price": "float",
            "volume": "integer",
        },
        partition_every="1day",
        target_rows_per_file=24,
        min_rows_per_file=12,
        max_rows_per_file=32,
    )

    rows = build_rows()
    result = ticks.write(rows)
    report = ticks.layout_report()
    segments = ticks.segments()
    first_segment = {
        key: segments[0][key]
        for key in ["logical_part", "segment_id", "part_key", "row_count", "time_min", "time_max"]
    }

    start = rows[ROWS_PER_DAY * 2]["ts"]
    end = rows[ROWS_PER_DAY * 3 - 1]["ts"]
    query = ticks.query(symbols="AAPL", start=start, end=end, order_by="ts")
    plan = query.explain()

    print("写入行数:", result.rows_seen)
    print("逻辑分区:", ticks.table_parts())
    print("物理 segment 数:", len(segments))
    print("首个 segment 概览:", first_segment)
    print("布局建议:", report.recommendation)
    print("实际扫描分区:", plan.scanned_partitions)
    print("扫描 segment:", [item["part_key"] for item in plan.scanned_segments])
    print("AAPL 第 3 天窗口行数:", len(query.to_list()))
    db.close()


if __name__ == "__main__":
    run()
