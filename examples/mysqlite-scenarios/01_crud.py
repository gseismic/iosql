"""mysqlite 场景 01：增删改查与冲突处理。

运行: python examples/mysqlite-scenarios/01_crud.py
"""

from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite-scenarios" / "01_crud"
DB_FILE = DB_DIR / "crud.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()
    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    orders = db["orders"]

    print("== 建表（首条写入自动建表，声明主键）")
    orders.insert(
        {"order_id": 1, "line_id": 1, "sku": "A", "qty": 2},
        pk=["order_id", "line_id"],
        ensure_table=True,
    )
    print("table_exists:", orders.table_exists(), "| pk:", orders.pk())

    print("== 批量写入 insert_all")
    orders.insert_all(
        [
            {"order_id": 1, "line_id": 2, "sku": "B", "qty": 1},
            {"order_id": 2, "line_id": 1, "sku": "C", "qty": 5},
            {"order_id": 2, "line_id": 2, "sku": "A", "qty": 3},
        ],
        batch_size=2,
    )
    print("count:", orders.count())

    print("== 主键冲突: replace 覆盖 / ignore 跳过")
    orders.insert({"order_id": 1, "line_id": 1, "sku": "A", "qty": 99}, replace=True)
    orders.insert({"order_id": 1, "line_id": 1, "sku": "X", "qty": 0}, ignore=True)
    print("覆盖后:", orders.select_one("*", where="order_id=1 AND line_id=1"))

    print("== update 按条件修改")
    orders.update({"qty": 10}, where="sku='C'")
    print("update 后:", orders.select_one("*", where="sku='C'"))

    print("== delete 条件删除 / delete_all 清空")
    orders.delete(where="order_id=2 AND line_id=2")
    print("删除后 count:", orders.count())

    print("== transaction：同组操作要么全提交要么全回滚")
    try:
        with db.transaction():
            orders.insert({"order_id": 3, "line_id": 1, "sku": "D", "qty": 1})
            raise RuntimeError("模拟失败")
    except RuntimeError:
        pass
    print("回滚后 count:", orders.count())
    with db.transaction():
        orders.insert({"order_id": 3, "line_id": 1, "sku": "D", "qty": 1})
    print("提交后 count:", orders.count())

    db.close()
    print("done ->", DB_FILE)


if __name__ == "__main__":
    run()
