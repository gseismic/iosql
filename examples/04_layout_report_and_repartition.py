"""高级：检查分区行数布局，并把数据重分区到新的数据库。"""

from pathlib import Path

from _example_support import reset_database as reset_demo_path

from iosql import Database, Segment


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SOURCE_PATH = RUNTIME_DIR / "04_layout_source.iosql"
TARGET_PATH = RUNTIME_DIR / "04_layout_target.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
TARGET_URI = f"sqlite://{TARGET_PATH}"


def reset_database():
    reset_demo_path(SOURCE_PATH)
    reset_demo_path(TARGET_PATH)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    with Database(SOURCE_URI) as db:
        # segment= 是行数阈值的唯一表达；旧的 *_rows_per_file 参数已废弃
        ticks = db.timeseries(
            "ticks",
            time="ts",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition_every="1day",
            segment=Segment.rows(max_rows=4, target_rows=4, min_rows=2),
        )

        base_ts = 1735689600000
        rows = [
            {"symbol": f"SYM{i % 3}", "ts": base_ts + i * 3_600_000, "price": 100 + i}
            for i in range(9)
        ]
        result = ticks.write(rows)
        report = ticks.layout_report()

        # with_partition 统一示范 partition= 完整表达；every= 为兼容快捷方式
        new_spec = ticks.spec().with_partition(partition="duration(ts,every=1hour)")
        repartitioned = ticks.repartition_to(TARGET_URI, spec=new_spec)

        with Database(TARGET_URI) as target_db:
            target = target_db.table("ticks")
            print("写入警告:", result.layout_warnings)
            print("布局建议:", report.recommendation)
            print("重分区后分区:", repartitioned.partitions_created)
            print("目标库前 3 行:", target.query(order_by="ts", limit=3).to_list())


if __name__ == "__main__":
    run()
