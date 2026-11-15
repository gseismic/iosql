"""用事务保证库存扣减与预留记录一起提交或回滚。

运行：在本文件所在目录执行 python 01_crud.py。
准备：已安装 iosql 及其默认依赖。
预期：失败后库存仍为 5；成功后库存为 3，并生成一条预留记录。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "01_crud"
DB_FILE = DB_DIR / "crud.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with Database(str(DB_FILE), verbose=0) as db:
        stock = db["stock"]
        stock.insert({"sku": "A", "qty": 5}, pk="sku", ensure_table=True)
        reservations = db["reservations"]
        reservations.create_table(
            [{"name": "id", "type": "integer"}, {"name": "qty", "type": "integer"}],
            pk="id",
        )

        # 模拟扣减之后、写入预留记录之前失败，事务会撤销库存变化。
        try:
            with db.transaction():
                stock.execute_params("UPDATE stock SET qty = qty - ? WHERE sku = ?", [2, "A"])
                raise RuntimeError("模拟预留失败")
        except RuntimeError as error:
            print(error)
        print("回滚后库存:", stock.select_one("qty")["qty"])

        # 两次写入复用同一事务连接；退出上下文时一并提交。
        with db.transaction():
            stock.execute_params("UPDATE stock SET qty = qty - ? WHERE sku = ?", [2, "A"])
            reservations.insert({"id": 1, "qty": 2})
        print("提交后库存:", stock.select_one("qty")["qty"])
        print("预留记录:", reservations.select_all("*"))
    print("数据位置:", DB_FILE)


if __name__ == "__main__":
    run()
