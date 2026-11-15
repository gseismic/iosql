"""按标的筛选行情，并用参数化 SQL 查询和聚合。

运行：在本文件所在目录执行 python 03_query_variants.py。
准备：已安装 iosql 及其默认依赖。
预期：输出 AAPL 行情、各标的均价及一条参数化查询结果。
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
    with Database(str(DB_FILE), verbose=0) as db:
        seed(db)
        bars = db["bars"]
        print("AAPL 前两条:", bars.select_all("*", where="symbol='AAPL'", order_by="ts", limit=2))
        print("按标的汇总:", bars.query_all(
            "SELECT symbol, COUNT(*) AS n, AVG(close) AS avg_close "
            "FROM bars GROUP BY symbol ORDER BY symbol"
        ))

        # 外部值通过占位符传入，不拼接到原生 SQL 中；fetchone 返回 sqlite3.Row。
        symbol, timestamp = "AAPL", 2
        row = bars.fetchone("SELECT close FROM bars WHERE symbol=? AND ts=?", [symbol, timestamp])
        print("参数化查询价格:", row["close"])
    print("数据位置:", DB_FILE)


if __name__ == "__main__":
    run()
