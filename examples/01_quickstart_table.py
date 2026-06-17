"""入门：直接用原始 Table + TableSpec 写入时间序列数据。"""

from pathlib import Path
import shutil

from iosql import Database, PartitionSpec, TableSpec, TimeColumn


DB_PATH = Path(__file__).resolve().parent / "runtime" / "01_quickstart_table.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    spec = TableSpec(
        primary_key=["symbol", "ts"],
        # time 是查询语义，partition 是物理布局；二者通常使用同一时间列。
        time=TimeColumn("ts", unit="ms", timezone="UTC"),
        partition=PartitionSpec.by_time("ts", every="1day"),
        columns={"symbol": "text", "ts": "integer", "price": "float"},
    )
    ticks = db.table("ticks", spec=spec)

    result = ticks.write([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
    ])

    query = ticks.query(
        symbols="AAPL",
        start=1735689600000,
        end=1735776000000,
        order_by="ts",
    )
    rows = query.to_list()
    plan = query.explain()

    print("写入行数:", result.rows_seen)
    print("分区:", ticks.table_parts())
    print("扫描分区:", plan.scanned_partitions)
    print("AAPL 查询结果:", rows)
    db.close()


if __name__ == "__main__":
    run()
