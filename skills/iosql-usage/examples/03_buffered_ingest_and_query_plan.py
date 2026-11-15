"""缓冲批量写入并查看查询分区裁剪。

运行：在本文件所在目录执行 python 03_buffered_ingest_and_query_plan.py。
准备：已安装 iosql 及其默认依赖。
预期：输出候选分区、实际扫描分区和行情。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "03_buffered.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        ticks = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1hour"),
        )

        base_ts = 1735689600000
        with ticks.buffered_writer(max_rows=4) as buffer:
            for hour in range(4):
                for symbol in ["AAPL", "MSFT"]:
                    buffer.append({
                        "symbol": symbol,
                        "ts": base_ts + hour * 3_600_000,
                        "price": 100 + hour * 2 + (0.5 if symbol == "MSFT" else 0),
                    })

        where = [
            {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]},
            {
                "field": "ts",
                "op": "between",
                "value": [base_ts + 3_600_000, base_ts + 2 * 3_600_000],
            },
        ]
        rows = ticks.select(
            where=where,
            order_by="ts desc",
            limit=3,
            to_dict=True,
        )
        plan = ticks.explain_select(where=where, order_by="ts desc", limit=3)

        print("候选分区:", plan.candidate_partitions)
        print("实际扫描分区:", plan.scanned_partitions)
        print("查询结果:", rows)


if __name__ == "__main__":
    run()
