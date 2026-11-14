"""使用 mysqlite 增删改查、处理冲突和事务。

运行：在本文件所在目录执行 python 01_crud.py。
准备：已安装 iosql 及其默认依赖。
预期：输出各操作结果及事务回滚、提交后的行数。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "01_crud"
DB_FILE = DB_DIR / "crud.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
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
