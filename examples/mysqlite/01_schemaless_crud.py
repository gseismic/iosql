"""使用 mysqlite 自动建表、动态加列并执行 CRUD。

运行：在本文件所在目录执行 python 01_schemaless_crud.py。
准备：已安装 iosql 及其默认依赖。
预期：输出表是否存在、列和剩余记录。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "01_schemaless_crud"
DB_FILE = DB_DIR / "demo.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)

    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    events = db["events"]

    events.insert({"id": 1, "kind": "click", "page": "/home"}, pk="id", ensure_table=True)
    events.insert({"id": 2, "kind": "scroll", "delta_y": 320}, ensure_column=True)
    events.update({"page": "/pricing"}, where="id=1")
    events.delete(where="id=2")

    rows = events.select_all("*", order_by="id")

    print("表已创建:", events.table_exists())
    print("列:", list(events.column_info()["column_type"].keys()))
    print("剩余行:", rows)
    db.close()


if __name__ == "__main__":
    run()
