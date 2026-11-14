"""缓冲写入 orderbook 快照并查询盘口。

运行：在本文件所在目录执行 python 02_orderbook_snapshots.py。
准备：已安装 iosql 及其默认依赖。
预期：输出分区、扫描计划和盘口窗口。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Segment


EXAMPLE_DIR = Path(__file__).resolve().parent
DB_PATH = EXAMPLE_DIR / "runtime" / uuid4().hex / "02_orderbook.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        book = db.timeseries(
            "orderbook_l2",
            time="ts",
            pk=["symbol", "venue", "ts", "side", "level"],
            columns={
                "symbol": "text",
                "venue": "text",
                "ts": "integer",
                "side": "text",
                "level": "integer",
                "price": "float",
                "size": "float",
            },
            partition_every="1hour",
            segment=Segment.rows(max_rows=300_000, target_rows=200_000),
        )

        base_ts = 1735689600000
        with book.buffered_writer(max_rows=8) as buffer:
            for tick in range(3):
                ts = base_ts + tick * 500
                for side, sign in [("bid", -1), ("ask", 1)]:
                    for level in range(1, 4):
                        buffer.append({
                            "symbol": "AAPL",
                            "venue": "NASDAQ",
                            "ts": ts,
                            "side": side,
                            "level": level,
                            "price": 187.0 + sign * level * 0.01,
                            "size": 1000 - level * 100 + tick * 10,
                        })

        query = book.query(
            symbols="AAPL",
            start=base_ts,
            end=base_ts + 1000,
            where="venue == 'NASDAQ'",
            order_by=["ts", "side", "level"],
            limit=8,
        )

        print("分区:", book.table_parts())
        print("扫描分区:", query.explain().scanned_partitions)
        print("盘口窗口:", query.to_list())


if __name__ == "__main__":
    run()
