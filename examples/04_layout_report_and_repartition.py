"""高级：检查分区行数布局，并把数据重分区到新的数据库。"""

from pathlib import Path
import shutil

from iosql import Database


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SOURCE_PATH = RUNTIME_DIR / "04_layout_source.iosql"
TARGET_PATH = RUNTIME_DIR / "04_layout_target.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
TARGET_URI = f"sqlite://{TARGET_PATH}"


def reset_database():
    shutil.rmtree(SOURCE_PATH, ignore_errors=True)
    shutil.rmtree(TARGET_PATH, ignore_errors=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(SOURCE_URI)
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=4,
        min_rows_per_file=2,
        max_rows_per_file=4,
    )

    base_ts = 1735689600000
    rows = [
        {"symbol": f"SYM{i % 3}", "ts": base_ts + i * 3_600_000, "price": 100 + i}
        for i in range(9)
    ]
    result = ticks.write(rows)
    report = ticks.layout_report()

    new_spec = ticks.spec().with_partition(every="1hour")
    repartitioned = ticks.repartition_to(TARGET_URI, spec=new_spec)
    target_db = Database(TARGET_URI)
    target = target_db.table("ticks")

    print("写入警告:", result.layout_warnings)
    print("布局建议:", report.recommendation)
    print("重分区后分区:", repartitioned.partitions_created)
    print("目标库前 3 行:", target.query(order_by="ts", limit=3).to_list())
    db.close()
    target_db.close()


if __name__ == "__main__":
    run()
