"""常用生产流：按字段全局排序并惰性逐行读取大结果集。"""

from pathlib import Path
import shutil

from iosql import Database, Partition, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / "13_streaming_ordered.iosql"
DB_URI = f"sqlite://{DB_PATH}"
BASE_TIME = 1735689600000


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

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
        rows = query.iter_rows(batch_size=2)
        first = next(rows)
        remaining = list(rows)
        ordered_times = [row["open_time"] for row in [first, *remaining]]
        assert ordered_times == sorted(ordered_times)

        print("首条结果:", first)
        print("后续结果数:", len(remaining))
        print("按 open_time 排序:", ordered_times)


if __name__ == "__main__":
    run()
