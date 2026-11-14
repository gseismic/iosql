"""逐行处理全局有序的大结果集。

运行：在本文件所在目录执行 python 13_streaming_ordered_query.py。
准备：已安装 iosql 及其默认依赖。
预期：输出首条记录、行数和有序校验结果。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from contextlib import closing
from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "13_streaming_ordered.iosql"
DB_URI = f"sqlite://{DB_PATH}"
BASE_TIME = 1735689600000


def show_ordered_rows(rows):
    """逐行处理，只保留上一条时间和计数。"""
    count = 0
    previous = None
    for row in rows:
        current = row["open_time"]
        if count == 0:
            print("首条结果:", row)
        else:
            assert previous <= current
        previous = current
        count += 1
    print("查询行数:", count)
    print("按 open_time 排序:", "是")


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        ticks = db.table(
            "ticks",
            pk=["symbol", "open_time"],
            time="open_time",
            symbol_column="symbol",
            columns={
                "symbol": "text",
                "open_time": "integer",
                "price": "float",
            },
            partition=Partition.duration("open_time", every="1hour"),
            segment=Segment.rows(max_rows=3, target_rows=2, min_rows=1),
            order_key="open_time",
        )
        ticks.write([
            {
                "symbol": "AAPL",
                "open_time": BASE_TIME + 2 * 3_600_000,
                "price": 102.0,
            },
            {
                "symbol": "MSFT",
                "open_time": BASE_TIME + 3_600_000,
                "price": 201.0,
            },
            {"symbol": "AAPL", "open_time": BASE_TIME, "price": 100.0},
            {
                "symbol": "AAPL",
                "open_time": BASE_TIME + 4 * 3_600_000,
                "price": 104.0,
            },
            {
                "symbol": "AAPL",
                "open_time": BASE_TIME + 3_600_000,
                "price": 101.0,
            },
            {
                "symbol": "AAPL",
                "open_time": BASE_TIME + 5 * 3_600_000,
                "price": 105.0,
            },
        ])

        # query() 只创建查询规格；iter_rows() 返回迭代器，不会先构造完整结果列表。
        query = ticks.query(symbols="AAPL", order_by="open_time")
        with closing(query.iter_rows(batch_size=2)) as rows:
            show_ordered_rows(rows)


if __name__ == "__main__":
    run()
