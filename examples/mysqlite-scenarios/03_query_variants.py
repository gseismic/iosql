"""mysqlite 场景 03：各类查询方法。

运行: python examples/mysqlite-scenarios/03_query_variants.py
"""

from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite-scenarios" / "03_query_variants"
DB_FILE = DB_DIR / "query.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def seed(db):
    bars = db["bars"]
    bars.create_table(
        [
            {"name": "symbol", "type": "text"},
            {"name": "ts", "type": "integer"},
            {"name": "close", "type": "float"},
        ],
        pk=["symbol", "ts"],
        table_comment="demo rows",
    )
    bars.insert_all(
        [
            {"symbol": "AAPL", "ts": t, "close": 100.0 + t}
            for t in range(1, 6)
        ]
        + [
            {"symbol": "MSFT", "ts": t, "close": 200.0 + t}
            for t in range(1, 4)
        ]
    )


def run():
    reset_database()
    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    seed(db)
    bars = db["bars"]

    print("== select_all / select_one：返回 dict 行")
    print(bars.select_all("*", where="symbol='AAPL'", order_by="ts", limit=2))
    print(bars.select_one(["ts", "close"], where="symbol='AAPL' AND ts=3"))

    print("== select：字段列表、limit=0、原生元组行")
    print(bars.select(["symbol", "close"], where="ts>=4", order_by="ts"))
    print("limit=0 ->", bars.select("*", limit=0))
    print("tuple 行 ->", bars.select("symbol, close", where="symbol='MSFT'", order_by="ts desc"))

    print("== count(where)")
    print("AAPL:", bars.count(where="symbol='AAPL'"), "| 全部:", bars.count())

    print("== query/query_all：任意 SQL 聚合")
    print(bars.query_all("SELECT symbol, COUNT(*) AS n, AVG(close) AS avg_close FROM bars GROUP BY symbol"))

    print("== fetchall/fetchone：底层游标行(sqlite3.Row)")
    row = bars.fetchone("SELECT close FROM bars WHERE symbol=? AND ts=?", ["AAPL", 2])
    print("fetchone:", tuple(row))
    print("fetchall:", [tuple(r) for r in bars.fetchall("SELECT symbol, ts FROM bars ORDER BY ts LIMIT 3")])

    print("== execute_params：参数化执行写语句")
    bars.execute_params("UPDATE bars SET close = close + ? WHERE symbol = ?", [1000.0, "MSFT"])
    print(bars.select_one("*", where="symbol='MSFT' AND ts=1"))

    db.close()
    print("done ->", DB_FILE)


if __name__ == "__main__":
    run()
