"""使用普通表和时间分区写入、条件查询。

运行：在本文件所在目录执行 python 01_quickstart_table.py。
准备：已安装 iosql 及其默认依赖。
预期：输出写入行数、分区、裁剪计划和 AAPL 行情。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "01_quickstart_table.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        ticks = db.table(
            "ticks",
            pk=["symbol", "ts"],
            partition=Partition.duration("ts", every="1day"),
            columns={"symbol": "text", "ts": "integer", "price": "float"},
        )

        result = ticks.write([
            {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
            {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
            {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
        ])

        rows = ticks.select(
            where="symbol == 'AAPL' and ts between 1735689600000 and 1735776000000",
            order_by="ts",
            to_dict=True,
        )
        plan = ticks.explain_select(
            where="symbol == 'AAPL' and ts between 1735689600000 and 1735776000000",
            order_by="ts",
        )

        print("写入行数:", result.rows_seen)
        print("分区:", ticks.table_parts())
        print("扫描分区:", plan.scanned_partitions)
        print("AAPL 查询结果:", rows)


if __name__ == "__main__":
    run()
