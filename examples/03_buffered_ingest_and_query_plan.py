"""进阶：批量缓冲写入，并查看查询计划中的分区裁剪结果。"""

from pathlib import Path
import shutil

from iosql import Database


DB_PATH = Path(__file__).resolve().parent / "runtime" / "03_buffered.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1hour",
        target_rows_per_file=1000,
        min_rows_per_file=100,
        max_rows_per_file=2000,
    )

    base_ts = 1735689600000
    with ticks.write_buffer(max_rows=4) as buffer:
        for hour in range(4):
            for symbol in ["AAPL", "MSFT"]:
                buffer.append({
                    "symbol": symbol,
                    "ts": base_ts + hour * 3_600_000,
                    "price": 100 + hour * 2 + (0.5 if symbol == "MSFT" else 0),
                })

    query = ticks.query(
        symbols=["AAPL", "MSFT"],
        start=base_ts + 3_600_000,
        end=base_ts + 2 * 3_600_000,
        order_by="ts desc",
        limit=3,
    )
    plan = query.explain()

    print("候选分区:", plan.candidate_partitions)
    print("实际扫描分区:", plan.scanned_partitions)
    print("查询结果:", query.to_list())
    db.close()


if __name__ == "__main__":
    run()
