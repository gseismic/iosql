"""高级：用字符串 partition 和 CLI 执行安全重分区。"""

from pathlib import Path
import subprocess
import sys

from _example_support import reset_database as reset_demo_path

from iosql import Database


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SOURCE_PATH = RUNTIME_DIR / "07_partition_string_source.iosql"
TARGET_PATH = RUNTIME_DIR / "07_partition_string_hourly.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
TARGET_URI = f"sqlite://{TARGET_PATH}"


def reset_database():
    reset_demo_path(SOURCE_PATH)
    reset_demo_path(TARGET_PATH)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    with Database(SOURCE_URI) as db:
        ticks = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition="duration(ts,every=1day)",
            order_key="ts",
        )
        base_ts = 1735689600000
        ticks.write([
            {"symbol": "AAPL", "ts": base_ts + hour * 3_600_000, "price": 187 + hour}
            for hour in range(4)
        ])
        print("源库分区:", ticks.spec().partition.to_string())

    command = [
        sys.executable,
        "-m",
        "iosql.cli",
        "repartition",
        "--source",
        SOURCE_URI,
        "--table",
        "ticks",
        "--partition",
        "duration(ts,every=1hour)",
        "--target",
        TARGET_URI,
    ]
    completed = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    )
    print("CLI 输出:", completed.stdout.strip())

    with Database(TARGET_URI) as target_db:
        target = target_db.table("ticks")
        print("目标库分区:", target.spec().partition.to_string())
        print("目标库分区键:", target.table_parts())


if __name__ == "__main__":
    run()
