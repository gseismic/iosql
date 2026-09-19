"""进阶：批量缓冲写入，并查看查询计划中的分区裁剪结果。"""

from pathlib import Path
import shutil

from iosql import Database, Partition, Segment


DB_PATH = Path(__file__).resolve().parent / "runtime" / "03_buffered.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    with Database(DB_URI) as db:
        ticks = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1hour"),
            segment=Segment.rows(max_rows=2000, target_rows=1000, min_rows=100),
            order_key="ts",
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
