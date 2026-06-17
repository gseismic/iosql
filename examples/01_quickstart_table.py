"""入门：用普通 Table + 自动分区写入可排序字段数据。"""

from pathlib import Path
import shutil

from iosql import Database, Partition


DB_PATH = Path(__file__).resolve().parent / "runtime" / "01_quickstart_table.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    ticks = db.table(
        "ticks",
        pk=["symbol", "ts"],
        partition=Partition.duration("ts", every="1day"),
        columns={"symbol": "text", "ts": "integer", "price": "float"},
    )

    result = ticks.insert_all([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
    ])

    where = [
        {"field": "symbol", "op": "==", "value": "AAPL"},
        {"field": "ts", "op": "between_and", "value": [1735689600000, 1735776000000]},
    ]
    rows = ticks.select(
        "*",
        where=where,
        order_by="ts",
        to_dict=True,
    )
    plan = ticks.explain_select(where=where, order_by="ts")

    print("写入行数:", result.rows_seen)
    print("分区:", ticks.table_parts())
    print("扫描分区:", plan.scanned_partitions)
    print("AAPL 查询结果:", rows)
    db.close()


if __name__ == "__main__":
    run()
