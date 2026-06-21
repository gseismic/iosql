"""入门：创建一个毫秒时间戳时序表，写入数据并按时间窗口查询。"""

from pathlib import Path
import shutil

from iosql import Database


DB_PATH = Path(__file__).resolve().parent / "runtime" / "01_quickstart.iosql"
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
        partition_every="1day",
    )

    result = ticks.write([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
    ])

    rows = ticks.query(
        symbols="AAPL",
        start=1735689600000,
        end=1735776000000,
        order_by="ts",
    ).to_list()

    print("写入行数:", result.rows_seen)
    print("分区:", ticks.table_parts())
    print("AAPL 查询结果:", rows)
    db.close()


if __name__ == "__main__":
    run()
