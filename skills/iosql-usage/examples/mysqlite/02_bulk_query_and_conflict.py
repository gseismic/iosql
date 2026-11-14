"""使用 mysqlite 批量写入、处理冲突并聚合查询。

运行：在本文件所在目录执行 python 02_bulk_query_and_conflict.py。
准备：已安装 iosql 及其默认依赖。
预期：输出主键、明细和按 SKU 汇总结果。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "02_bulk_query"
DB_FILE = DB_DIR / "demo.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)

    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    orders = db["orders"]

    orders.insert_all([
        {"order_id": 1, "line_id": 1, "sku": "A", "qty": 2},
        {"order_id": 1, "line_id": 2, "sku": "B", "qty": 1},
        {"order_id": 2, "line_id": 1, "sku": "A", "qty": 5},
    ], ensure_table=True, pk=["order_id", "line_id"])

    orders.insert_all([
        {"order_id": 1, "line_id": 1, "sku": "A", "qty": 9},
    ], replace=True)
    orders.insert_all([
        {"order_id": 1, "line_id": 1, "sku": "A", "qty": 99},
    ], ignore=True)

    summary = orders.query_all(
        "SELECT sku, SUM(qty) AS total_qty FROM orders GROUP BY sku ORDER BY sku"
    )
    rows = orders.select_all("*", order_by="order_id,line_id")

    print("主键:", orders.pk())
    print("明细:", rows)
    print("汇总:", summary)
    db.close()


if __name__ == "__main__":
    run()
