"""高级：doctor 对历史 partition_key 表优先给出时间分区建议。"""

from pathlib import Path
import shutil
import subprocess
import sys

from iosql import Database, PhysicalLayout, TableContract, TimeColumn


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
SOURCE_PATH = RUNTIME_DIR / "08_doctor_time_first.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"


def reset_database():
    shutil.rmtree(SOURCE_PATH, ignore_errors=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(SOURCE_URI)
    spec = TableContract(
        primary_key=["symbol", "date"],
        time=TimeColumn("date", unit="date", timezone="Asia/Shanghai"),
        partition="range(partition_key,width=1)",
        columns={
            "symbol": "text",
            "date": "text",
            "partition_key": "integer",
            "close": "float",
        },
        order_key="date",
        physical_layout=PhysicalLayout(
            target_rows_per_file=8,
            min_rows_per_file=5,
            max_rows_per_file=10,
        ),
    )
    table = db.table("daily_bars", spec=spec)
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
    db.close()

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
