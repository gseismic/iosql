"""高级：doctor 对历史 partition_key 表优先给出时间分区建议。

高级用法仍可直接构造 TableContract/PhysicalLayout；本例演示用
db.table() 快捷参数表达同样的契约。
"""

from pathlib import Path
import shutil
import subprocess
import sys

from iosql import Database, Segment, TimeColumn


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SOURCE_PATH = RUNTIME_DIR / "08_doctor_time_first.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"


def reset_database():
    shutil.rmtree(SOURCE_PATH, ignore_errors=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    with Database(SOURCE_URI) as db:
        table = db.table(
            "daily_bars",
            pk=["symbol", "date"],
            time=TimeColumn("date", unit="date", timezone="Asia/Shanghai"),
            partition="range(partition_key,width=1)",
            columns={
                "symbol": "text",
                "date": "text",
                "partition_key": "integer",
                "close": "float",
            },
            order_key="date",
            segment=Segment.rows(max_rows=10, target_rows=8, min_rows=5),
        )
        rows = []
        for day in range(4):
            date = f"2025-01-{day + 1:02d}"
            rows.extend([
                {
                    "symbol": "AAPL",
                    "date": date,
                    "partition_key": day,
                    "close": 100 + day,
                },
                {
                    "symbol": "MSFT",
                    "date": date,
                    "partition_key": day,
                    "close": 200 + day,
                },
            ])
        table.write(rows)

    command = [
        sys.executable,
        "-m",
        "iosql.cli",
        "doctor",
        "--source",
        SOURCE_URI,
        "--table",
        "daily_bars",
    ]
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    print(completed.stdout.strip())


if __name__ == "__main__":
    run()
