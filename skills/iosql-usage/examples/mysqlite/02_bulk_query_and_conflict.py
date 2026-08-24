"""mysqlite：批量写入、聚合查询和冲突处理。"""

from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite" / "02_bulk_query"
DB_FILE = DB_DIR / "demo.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

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
    reset_database()


if __name__ == "__main__":
    run()
