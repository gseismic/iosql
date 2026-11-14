"""使用 CLI 和分区表达式将数据重分区到新库。

运行：在本文件所在目录执行 python 07_partition_string_repartition_cli.py。
准备：已安装 iosql 及其默认依赖。
预期：输出 CLI 结果和目标库分区。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from iosql import Database


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime" / f"07_repartition_{uuid4().hex}"
SOURCE_PATH = RUNTIME_DIR / "07_partition_string_source.iosql"
TARGET_PATH = RUNTIME_DIR / "07_partition_string_hourly.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
TARGET_URI = f"sqlite://{TARGET_PATH}"


def run():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

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
        print("演示数据目录:", RUNTIME_DIR)


if __name__ == "__main__":
    run()
