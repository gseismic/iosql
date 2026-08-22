"""金融进阶：批量写入 orderbook 快照，并按时间窗口读取盘口。"""

from pathlib import Path
import shutil

from iosql import Database, Segment


EXAMPLES_DIR = Path(__file__).resolve().parents[1]
DB_PATH = EXAMPLES_DIR / "runtime" / "finance" / "02_orderbook.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

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
        with book.write_buffer(max_rows=8) as buffer:
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
