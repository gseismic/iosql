"""对历史分区表运行 doctor 并查看时间分区建议。

运行：在本文件所在目录执行 python 08_doctor_time_first_suggestions.py。
准备：已安装 iosql 及其默认依赖。
预期：输出诊断问题和时间优先的分区建议。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4
import subprocess
import sys

from iosql import Database, Segment, TimeColumn


RUNTIME_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex
SOURCE_PATH = RUNTIME_DIR / "08_doctor_time_first.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"


def run():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

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
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.stderr:
        print(completed.stderr.strip(), file=sys.stderr)
    if completed.returncode == 2:
        print("doctor 已发现待处理问题，详见上方建议。")
    elif completed.returncode != 0:
        print(f"doctor 执行失败，退出码: {completed.returncode}", file=sys.stderr)
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    run()
