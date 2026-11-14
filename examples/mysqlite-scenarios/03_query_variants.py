"""使用 mysqlite 执行条件、聚合和参数化查询。

运行：在本文件所在目录执行 python 03_query_variants.py。
准备：已安装 iosql 及其默认依赖。
预期：输出不同查询方法的结果。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "03_query_variants"
DB_FILE = DB_DIR / "query.sqlite3"


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
    DB_DIR.mkdir(parents=True, exist_ok=True)
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
